"""
Tests for the Claude Agent SDK adapter (rewind_sdk/adapters/claude_agent.py).

Must run with neither Docker nor claude_agent_sdk installed, per
.claude/CLAUDE.md. Hooks are plain async callables here (claude_agent_sdk is
absent, so rewind_hooks()'s HookMatcher fallback returns bare callables), so
they're driven directly with asyncio.run(...) and hand-built input_data dicts.

FakeEngine is copied from tests/test_verification.py's richer version
(run_cmd_capturing + destroy_sandbox + commit), following this repo's existing
per-file convention of duplicating the stub rather than sharing a conftest.
"""

import asyncio

import pytest

import rewind_sdk
from rewind_sdk.adapters.claude_agent import (
    raise_if_halted,
    rewind_hooks,
    sandbox_tool,
    sdk_messages_to_dicts,
)
from rewind_sdk.verification import (
    VerificationHaltError,
    VerificationResult,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Shared fixtures/helpers
# ---------------------------------------------------------------------------

class FakeEngine:
    """Minimal engine stub; avoids any Docker calls."""

    def __init__(self):
        self.checkpoint_history = []
        self.rolled_back_to = None
        self._stdout = ""
        self._stderr = ""
        self._returncode = 1
        self._stdout_sequence: list[str] = []
        self.destroyed = False
        self.committed = False

    def load_metadata(self):
        return True

    def run_cmd(self, cmd):
        if self._returncode != 0:
            raise RuntimeError(f"Command failed: {cmd}")
        return self._stdout.strip()

    def run_cmd_capturing(self, cmd, timeout=None):
        if self._stdout_sequence:
            stdout = self._stdout_sequence.pop(0)
            return stdout, self._stderr, self._returncode
        return self._stdout, self._stderr, self._returncode

    def create_checkpoint(self, label):
        self.checkpoint_history.append(label)

    def rollback_to_checkpoint(self, label):
        self.rolled_back_to = label

    def destroy_sandbox(self):
        self.destroyed = True

    def commit(self, workspace):
        self.committed = True


def _make_session(**kwargs):
    engine = FakeEngine()
    session = rewind_sdk.RewindSession(
        engine=engine,
        destroy_on_exit=False,
        mode="agent",
        **kwargs,
    )
    return session, engine


def _make_halt_error():
    return VerificationHaltError(
        "halted",
        checkpoint="good",
        verifier_command="fake",
        last_result=VerificationResult(
            status=VerificationStatus.UNKNOWN,
            raw_output={},
            notes="unknown",
        ),
    )


def _run(coro):
    return asyncio.run(coro)


def _hook_fn(entry):
    """Unwrap a hook callable from a real ``HookMatcher`` when the SDK is installed.

    ``rewind_hooks`` wraps callbacks in ``HookMatcher(hooks=[...])`` when
    ``claude_agent_sdk`` is present, and returns bare callables otherwise.
    """
    wrapped = getattr(entry, "hooks", None)
    return wrapped[0] if wrapped is not None else entry


def _call_tool(tool_obj, args):
    """Invoke a sandbox tool whether it's a bare coroutine function or a real ``SdkMcpTool``."""
    handler = getattr(tool_obj, "handler", None)
    return (handler or tool_obj)(args)


def _pre_tool_use(hooks):
    return _hook_fn(hooks["PreToolUse"][0])


def _post_tool_use(hooks):
    return _hook_fn(hooks["PostToolUse"][0])


def _post_tool_use_failure(hooks):
    return _hook_fn(hooks["PostToolUseFailure"][0])


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------

def test_pre_tool_use_checkpoints_normal_tool():
    session, engine = _make_session()
    session._started = True
    session.auto_checkpoint(trigger="before_tool_call")
    hooks = rewind_hooks(session)

    input_data = {
        "tool_name": "run_sql",
        "tool_input": {},
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "/",
    }
    result = _run(_pre_tool_use(hooks)(input_data, "tool_use_1", None))

    assert result == {}
    assert len(engine.checkpoint_history) == 1
    assert "run_sql" in engine.checkpoint_history[0]


def test_pre_tool_use_skips_mcp_rewind_tools():
    session, engine = _make_session()
    session._started = True
    session.auto_checkpoint(trigger="before_tool_call")
    hooks = rewind_hooks(session)

    input_data = {
        "tool_name": "mcp__rewind__run_sql",
        "tool_input": {},
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "/",
    }
    result = _run(_pre_tool_use(hooks)(input_data, "tool_use_1", None))

    assert result == {}
    assert engine.checkpoint_history == []


def test_pre_tool_use_denies_builtin_write():
    session, engine = _make_session()
    session._started = True
    session.auto_checkpoint(trigger="before_tool_call")
    hooks = rewind_hooks(session, deny_builtin_writes=True)

    input_data = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/etc/passwd"},
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "/",
    }
    result = _run(_pre_tool_use(hooks)(input_data, "tool_use_1", None))

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mcp__rewind__" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert engine.checkpoint_history == []


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------

def test_post_tool_use_error_triggers_rollback():
    session, engine = _make_session()
    session._started = True
    session.auto_checkpoint(trigger="before_tool_call")
    session.memory.snapshot("pre")
    engine.checkpoint_history.append("pre")
    session.auto_rollback("exception", to="pre")
    hooks = rewind_hooks(session)

    pre_input = {
        "tool_name": "run_migration",
        "tool_input": {},
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "/",
    }
    _run(_pre_tool_use(hooks)(pre_input, "tool_use_1", None))
    assert len(engine.checkpoint_history) == 2  # "pre" + the before-call auto-checkpoint

    post_input = {
        "tool_name": "run_migration",
        "tool_response": {"is_error": True, "content": "boom"},
        "hook_event_name": "PostToolUse",
    }
    result = _run(_post_tool_use(hooks)(post_input, "tool_use_1", None))

    assert engine.rolled_back_to == "pre"
    notice = result["hookSpecificOutput"]["additionalContext"]
    assert "[REWIND]" in notice
    assert "pre" in notice
    assert result["systemMessage"] == notice


def test_post_tool_use_success_no_rollback():
    session, engine = _make_session()
    session._started = True
    hooks = rewind_hooks(session)

    post_input = {
        "tool_name": "run_sql",
        "tool_response": {"is_error": False, "content": "ok"},
        "hook_event_name": "PostToolUse",
    }
    result = _run(_post_tool_use(hooks)(post_input, "tool_use_1", None))

    assert result == {}
    assert engine.rolled_back_to is None


# ---------------------------------------------------------------------------
# sandbox_tool
# ---------------------------------------------------------------------------

def test_sandbox_tool_rollback_on_error_false_no_rollback():
    session, engine = _make_session()
    session._started = True
    session.memory.snapshot("good")
    engine.checkpoint_history.append("good")
    session.auto_rollback("exception", to="good")

    @sandbox_tool(session, "run_sql", "Read-only query", {"query": str}, rollback_on_error=False)
    async def run_sql(args):
        return session.run("SELECT bad")

    result = _run(_call_tool(run_sql, {"query": "SELECT bad"}))

    assert result["is_error"] is True
    assert "ERROR" in result["content"][0]["text"]
    assert engine.rolled_back_to is None
    assert session.last_auto_rollback is None


def test_sandbox_tool_halt_error_is_stashed_and_reraised():
    session, _engine = _make_session()
    session._started = True
    rewind_hooks(session)  # installs the halt box on the session
    halt = _make_halt_error()

    @sandbox_tool(session, "run_migration", "Runs a migration", {})
    async def run_migration(args):
        raise halt

    result = _run(_call_tool(run_migration, {}))

    assert result["is_error"] is True
    assert str(halt) in result["content"][0]["text"]

    box = session._claude_agent_halt_box
    with pytest.raises(VerificationHaltError):
        raise_if_halted(box)


def test_hook_halt_error_stops_continuation_and_reraises():
    session, _engine = _make_session()
    session._started = True
    halt = _make_halt_error()

    def _raise_halt(*, error=None, messages=None):
        raise halt

    session.on_tool_result = _raise_halt
    hooks = rewind_hooks(session)
    box = session._claude_agent_halt_box

    post_input = {
        "tool_name": "run_migration",
        "tool_response": {"is_error": True, "content": "boom"},
        "hook_event_name": "PostToolUse",
    }
    result = _run(_post_tool_use(hooks)(post_input, "tool_use_1", None))

    assert result == {"continue_": False, "systemMessage": str(halt)}
    with pytest.raises(VerificationHaltError):
        raise_if_halted(box)


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------

class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class UserMessage:
    def __init__(self, content, uuid=None):
        self.content = content
        self.uuid = uuid


class AssistantMessage:
    def __init__(self, content, uuid=None):
        self.content = content
        self.uuid = uuid


def test_sdk_messages_to_dicts_round_trips_through_session():
    messages = [
        UserMessage(content="Please migrate the schema.", uuid="u1"),
        AssistantMessage(
            content=[
                TextBlock(text="I'll run the migration."),
                ToolUseBlock(id="call_1", name="run_migration", input={"path": "schema.sql"}),
            ],
            uuid="a1",
        ),
    ]

    dicts = sdk_messages_to_dicts(messages)

    assert dicts[0]["role"] == "user"
    assert dicts[0]["content"] == "Please migrate the schema."
    assert dicts[1]["role"] == "assistant"
    assert dicts[1]["tool_calls"] == [
        {"id": "call_1", "name": "run_migration", "args": {"path": "schema.sql"}}
    ]

    session = rewind_sdk.RewindSession(destroy_on_exit=False)
    synced = session.sync_memory(dicts)

    assert synced == dicts
    assert session.get_messages() == dicts


# ---------------------------------------------------------------------------
# checkpoint -> mutate -> rollback
# ---------------------------------------------------------------------------

def test_checkpoint_mutate_rollback_cycle():
    session, _engine = _make_session()
    session._started = True

    session.checkpoint("pre_migration", messages=[{"role": "user", "content": "Start migration."}])
    session.sync_memory(
        [
            {"role": "user", "content": "Start migration."},
            {"role": "assistant", "content": "Oops, broke it."},
        ]
    )

    resumed = session.rollback("pre_migration", patch_notes="Migration failed verification.")

    assert resumed[-1]["role"] == "system"
    assert "rolled back to checkpoint [pre_migration]" in resumed[-1]["content"]
    assert "Migration failed verification" in resumed[-1]["content"]

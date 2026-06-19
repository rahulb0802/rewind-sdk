import shutil
import subprocess

import pytest

import rewind_sdk


class FakeEngine:
    def __init__(self):
        self.checkpoint_history = []
        self.rolled_back_to = None

    def load_metadata(self):
        return True

    def run_cmd(self, cmd):
        raise RuntimeError(f"Command failed: {cmd}")

    def create_checkpoint(self, label):
        self.checkpoint_history.append(label)

    def rollback_to_checkpoint(self, label):
        self.rolled_back_to = label


def test_sync_memory_tracks_messages_without_manual_indices():
    session = rewind_sdk.RewindSession(destroy_on_exit=False)
    messages = [
        {"role": "user", "content": "Create a checkpointable state."},
        {"role": "assistant", "content": "State is ready."},
    ]

    synced = session.sync_memory(messages)

    assert synced == messages
    assert session.get_messages() == messages
    assert session.memory.get_messages() == messages


def test_declarative_triggers_checkpoint_and_rollback_without_manual_calls():
    engine = FakeEngine()
    session = rewind_sdk.RewindSession(engine=engine, destroy_on_exit=False)
    messages = [
        {"role": "user", "content": "Refactor auth."},
        {"role": "assistant", "content": "Starting."},
    ]
    failed_messages = messages + [
        {"role": "tool", "content": "pytest failed", "metadata": "run_tests"},
    ]

    session.auto_checkpoint(trigger="before_tool_call", keep_last=2)
    session.auto_rollback("test_failure", "exception", to="latest", test_command="pytest")

    session.on_tool_call(messages=messages, tool_name="read_file")
    session.on_tool_call(messages=messages, tool_name="write_file")
    session.on_tool_call(messages=messages, tool_name="run_tests")
    session.sync_memory(failed_messages)

    with pytest.raises(RuntimeError):
        session.run_tests("pytest")

    assert len(session._auto_labels) == 2
    assert engine.rolled_back_to == session._auto_labels[-1]
    assert session.last_auto_rollback["event"] == "test_failure"
    assert len(session.last_auto_rollback["messages"]) == len(messages) + 1
    assert "pytest failed" not in str(session.last_auto_rollback["messages"])


def docker_available():
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "version"],
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode == 0


@pytest.mark.skipif(not docker_available(), reason="Docker is required for Rewind integration tests")
def test_session_lifecycle_restores_filesystem_and_memory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "seed.txt").write_text("base\n", encoding="utf-8")

    messages = [
        {"role": "system", "content": "You are editing a sandboxed project."},
        {"role": "user", "content": "Create the stable config."},
        {"role": "assistant", "content": "Created config/settings.txt."},
    ]
    failed_messages = messages + [
        {"role": "user", "content": "Break the config."},
        {"role": "assistant", "content": "Overwrote it with bad data."},
    ]

    with rewind_sdk.session("rewind_pytest_session", workspace=str(workspace)) as session:
        assert session.run("cat seed.txt") == "base"

        session.write_file("config/settings.txt", "version=1\n")
        assert session.read_file("config/settings.txt") == "version=1\n"

        session.sync_memory(messages)
        session.checkpoint("stable")

        session.write_file("config/settings.txt", "corrupt=true\n")
        assert session.read_file("config/settings.txt") == "corrupt=true\n"
        session.sync_memory(failed_messages)

        resumed = session.rollback("stable", patch_notes="Rejected corrupt config write.")

        assert session.read_file("config/settings.txt") == "version=1\n"
        assert len(resumed) == len(messages) + 1
        assert not any("bad data" in message["content"] for message in resumed)
        assert "Rejected corrupt config write" in resumed[-1]["content"]

def test_auto_rollback_silently_noops_when_no_checkpoint_exists():
    """
    Edge case: if an exception happens before any checkpoint was ever taken,
    _maybe_auto_rollback catches the ValueError from _resolve_label("latest")
    and returns None, and then last_auto_rollback stays None; just need to confirm its like this
    """
    engine = FakeEngine()
    session = rewind_sdk.RewindSession(engine=engine, destroy_on_exit=False)

    session.auto_rollback("exception")
    # no checkpoint() or on_tool_call() before this point

    with pytest.raises(RuntimeError):
        session.run("some_command")  # FakeEngine.run_cmd always raises

    # The "self-healing" rollback does nothing
    assert session.last_auto_rollback is None
    assert engine.rolled_back_to is None


def test_rollback_truncation_misses_nested_dangling_tool_call():
    """
    Edge case: memory.MemoryStore.rollback() only looks one message back
    to drop a dangling tool-call message. If a checkpoint is
    taken while an earlier tool-call message is still dangling underneath
    a more recent tool-call (for example a nested tool call),
    only the most recent one gets truncated, so make sure this doesn't happen (while loop fix should work)
    """
    engine = FakeEngine()
    session = rewind_sdk.RewindSession(engine=engine, destroy_on_exit=False)
    session.auto_checkpoint(trigger="before_tool_call", keep_last=2)
    session.auto_rollback("exception", to="latest")

    base_messages = [
        {"role": "user", "content": "Refactor auth and run tests."},
    ]

    # call tool_a
    messages_after_tool_a_call = base_messages + [
        {"role": "assistant", "content": "Calling tool_a.", "tool_calls": [{"id": "call_a", "name": "tool_a"}]},
    ]
    session.on_tool_call(messages=messages_after_tool_a_call, tool_name="tool_a")
    checkpoint_a = session._auto_labels[-1]

    # print debugs
    print("\n--- AFTER tool_a checkpoint ---")
    print("auto_labels:", session._auto_labels)
    print("memory._snapshots:", session.memory._snapshots)
    print("memory._order:", session.memory._order)
    print("memory._messages:", session.memory._messages)

    # call b
    messages_after_tool_b_call = messages_after_tool_a_call + [
        {"role": "assistant", "content": "Calling tool_b.", "tool_calls": [{"id": "call_b", "name": "tool_b"}]},
    ]
    session.on_tool_call(messages=messages_after_tool_b_call, tool_name="tool_b")
    checkpoint_b = session._auto_labels[-1]

    # print debugs for tool_b
    print("\n--- AFTER tool_b checkpoint ---")
    print("auto_labels:", session._auto_labels)
    print("memory._snapshots:", session.memory._snapshots)
    print("memory._order:", session.memory._order)
    print("memory._messages:", session.memory._messages)

    # crash tool_b
    session.on_tool_result(error=RuntimeError("tool_b crashed"))

    print("\n--- AFTER on_tool_result (post-rollback) ---")
    print("last_auto_rollback:", session.last_auto_rollback)
    print("engine.rolled_back_to:", engine.rolled_back_to)
    print("memory._messages:", session.memory._messages)

    resumed = session.get_messages()
    print("\n--- resumed ---")
    print(resumed)

    dangling = [m for m in resumed if m.get("role") == "assistant" and m.get("tool_calls")]
    assert not dangling, f"Found dangling tool-call messages: {dangling}"

def test_rollback_truncation_may_remove_already_resolved_tool_call():
    """
    Edge case: MemoryStore.rollback()'s walk-back loop only checks
    "is this an assistant message with tool_calls?" to decide if a message
    is dangling. It has no way to tell a resolved tool call (one that has a
    matching tool-response message right after it) apart
    """
    engine = FakeEngine()
    session = rewind_sdk.RewindSession(engine=engine, destroy_on_exit=False)
    session.auto_checkpoint(trigger="before_tool_call", keep_last=2)
    session.auto_rollback("exception", to="latest")

    base_messages = [
        {"role": "user", "content": "Refactor auth and run tests."},
    ]

    # tool_a is called and fully resolved (call + matching tool response).
    messages_after_tool_a_resolved = base_messages + [
        {"role": "assistant", "content": "Calling tool_a.", "tool_calls": [{"id": "call_a", "name": "tool_a"}]},
        {"role": "tool", "content": "tool_a succeeded.", "metadata": "call_a"},
    ]

    # tool_b is called next and will crash (dangling).
    messages_after_tool_b_call = messages_after_tool_a_resolved + [
        {"role": "assistant", "content": "Calling tool_b.", "tool_calls": [{"id": "call_b", "name": "tool_b"}]},
    ]
    session.on_tool_call(messages=messages_after_tool_b_call, tool_name="tool_b")

    print("\n--- BEFORE crash ---")
    print("memory._messages:", session.memory._messages)

    session.on_tool_result(error=RuntimeError("tool_b crashed"))

    resumed = session.get_messages()
    print("\n--- resumed ---")
    print(resumed)

    # tool_a's call message should survive, it was already resolved, not dangling
    tool_a_call_present = any(
        m.get("role") == "assistant" and m.get("tool_calls") and m["tool_calls"][0]["id"] == "call_a"
        for m in resumed
    )
    assert tool_a_call_present, (
        "tool_a's already-resolved call message was removed during rollback. "
        "The walk-back loop in MemoryStore.rollback() cannot distinguish a "
        "resolved tool call from a dangling one, it only checks for the "
        "presence of `tool_calls`, not whether a matching tool-response "
        "message follows it."
    )

    # tool_a's matching tool and response should also still be there
    tool_a_response_present = any(
        m.get("role") == "tool" and m.get("metadata") == "call_a" for m in resumed
    )
    assert tool_a_response_present, "tool_a's resolved tool-response message was removed during rollback."
"""Claude Agent SDK adapter for Rewind.

Unlike LangGraph, the Agent SDK has no graph object to wrap: you configure
``ClaudeAgentOptions`` and drive ``query()`` / ``ClaudeSDKClient`` yourself.
This module exports plain functions instead of an owning wrapper class, per
the adapter contract in ``.claude/CLAUDE.md``.

Every ``claude_agent_sdk`` import below is lazy and guarded so that
``import rewind_sdk`` and the test suite work with the SDK absent.
"""

from ..verification import VerificationHaltError

BUILTIN_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")


# ---------------------------------------------------------------------------
# 1a. Message conversion
# ---------------------------------------------------------------------------

def _content_block_to_dict(block):
    """Duck-type a single Agent SDK content block into a plain dict."""
    kind = type(block).__name__

    if kind == "ToolUseBlock":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "input": getattr(block, "input", None),
        }
    if kind == "ToolResultBlock":
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "content": getattr(block, "content", None),
            "is_error": getattr(block, "is_error", None),
        }
    if kind == "ThinkingBlock":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", None),
            "signature": getattr(block, "signature", None),
        }
    # TextBlock and anything unrecognized degrade to plain text.
    return {"type": "text", "text": getattr(block, "text", str(block))}


def _sdk_message_content(message):
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_content_block_to_dict(block) for block in content]
    return content


def _tool_use_blocks(message):
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    tool_calls = [
        {
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "args": getattr(block, "input", None),
        }
        for block in content
        if type(block).__name__ == "ToolUseBlock"
    ]
    return tool_calls or None


def sdk_messages_to_dicts(messages):
    """Convert Agent SDK message objects into the dict shape MemoryStore stores.

    Duck-types on ``type(message).__name__`` rather than ``isinstance`` so
    callers can pass hand-built stub objects without ``claude_agent_sdk``
    installed, mirroring ``langgraph.messages_to_dicts``.
    """
    dicts = []
    for message in messages or []:
        if isinstance(message, dict):
            dicts.append(dict(message))
            continue

        kind = type(message).__name__
        if kind == "SystemMessage":
            role = "system"
            content = getattr(message, "data", None)
        elif kind == "ResultMessage":
            role = "system"
            content = getattr(message, "result", None)
        elif kind == "UserMessage":
            role = "user"
            content = _sdk_message_content(message)
        elif kind == "AssistantMessage":
            role = "assistant"
            content = _sdk_message_content(message)
        else:
            role = "user"
            content = _sdk_message_content(message)

        dicts.append(
            {
                "role": role,
                "content": content,
                "name": None,
                "id": getattr(message, "uuid", None) or getattr(message, "message_id", None),
                "tool_calls": _tool_use_blocks(message),
                "metadata": getattr(message, "session_id", None),
            }
        )
    return dicts


class HaltBox:
    """Holds the first VerificationHaltError seen by a set of hooks/tools.

    Exceptions raised inside an async hook or MCP tool callback are likely to
    be swallowed by the SDK transport rather than propagated, so the halt is
    stashed here and re-raised once control returns to plain Python code.
    """

    def __init__(self):
        self.halt = None

    def stash(self, halt):
        if self.halt is None:
            self.halt = halt

    def raise_if_halted(self):
        if self.halt is not None:
            halt, self.halt = self.halt, None
            raise halt


def raise_if_halted(box):
    box.raise_if_halted()


async def track_stream(session, stream):
    """Yield each message through untouched while keeping rewind memory synced.

    Re-raises a stashed VerificationHaltError (see ``HaltBox``) once the
    underlying stream ends.
    """
    box = getattr(session, "_claude_agent_halt_box", None)
    history = []
    async for message in stream:
        history.append(message)
        session.sync_memory(sdk_messages_to_dicts(history))
        yield message
    if box is not None:
        box.raise_if_halted()


# ---------------------------------------------------------------------------
# 1b. Hooks
# ---------------------------------------------------------------------------

def _tool_result_error(tool_response):
    if isinstance(tool_response, dict):
        if tool_response.get("is_error"):
            return tool_response.get("content") or "Tool reported an error."
        return None
    if getattr(tool_response, "is_error", False):
        return getattr(tool_response, "content", None) or "Tool reported an error."
    return None


def rewind_hooks(session, *, deny_builtin_writes=True, skip_prefix="mcp__rewind__", halt_box=None):
    """Build the ``ClaudeAgentOptions(hooks=...)`` dict for a rewind session."""
    if halt_box is None:
        halt_box = HaltBox()
    session._claude_agent_halt_box = halt_box

    async def pre_tool_use(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "")

        if tool_name.startswith(skip_prefix):
            return {}

        if deny_builtin_writes and tool_name in BUILTIN_WRITE_TOOLS:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"'{tool_name}' runs on the host and cannot be rolled back. "
                        f"Use the sandboxed '{skip_prefix}*' tools instead, which "
                        "execute inside the checkpointed rewind container."
                    ),
                }
            }

        session.on_tool_call(tool_name=tool_name)
        return {}

    def _report_result(input_data, error):
        try:
            session.on_tool_result(error=error)
        except VerificationHaltError as halt:
            halt_box.stash(halt)
            return {"continue_": False, "systemMessage": str(halt)}

        notice = session._consume_pending_rollback_notice()
        if not notice:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": input_data.get("hook_event_name", "PostToolUse"),
                "additionalContext": notice,
            },
            "systemMessage": notice,
        }

    async def post_tool_use(input_data, tool_use_id, context):
        error = _tool_result_error(input_data.get("tool_response"))
        return _report_result(input_data, error)

    async def post_tool_use_failure(input_data, tool_use_id, context):
        error = input_data.get("error") or "Tool call failed."
        return _report_result(input_data, error)

    try:
        from claude_agent_sdk import HookMatcher
    except ImportError:
        HookMatcher = None

    def _matcher(hooks):
        if HookMatcher is None:
            return hooks
        return [HookMatcher(hooks=hooks)]

    return {
        "PreToolUse": _matcher([pre_tool_use]),
        "PostToolUse": _matcher([post_tool_use]),
        "PostToolUseFailure": _matcher([post_tool_use_failure]),
    }


# ---------------------------------------------------------------------------
# 1c. Sandboxed tools
# ---------------------------------------------------------------------------

def _normalize_tool_result(result):
    if isinstance(result, dict) and "content" in result:
        return result
    if isinstance(result, str):
        return {"content": [{"type": "text", "text": result}]}
    return {"content": [{"type": "text", "text": str(result)}]}


def sandbox_tool(session, name, description, input_schema, *, rollback_on_error=True):
    """Async analogue of ``RewindSession.tool()`` for Agent SDK custom tools."""
    halt_box = getattr(session, "_claude_agent_halt_box", None)

    def decorator(func):
        async def wrapper(args):
            session.on_tool_call(tool_name=name)
            prev_suppressed = session._rollback_suppressed
            session._rollback_suppressed = not rollback_on_error
            prev_in_tool = session._in_tool_execution
            session._in_tool_execution = True
            try:
                result = await func(args)
            except VerificationHaltError as halt:
                if halt_box is not None:
                    halt_box.stash(halt)
                return {
                    "content": [{"type": "text", "text": str(halt)}],
                    "is_error": True,
                }
            except RuntimeError as exc:
                notice = session._consume_pending_rollback_notice()
                text = f"ERROR: {exc}\n{notice}" if notice else f"ERROR: {exc}"
                return {"content": [{"type": "text", "text": text}], "is_error": True}
            finally:
                session._in_tool_execution = prev_in_tool
                session._rollback_suppressed = prev_suppressed
            return _normalize_tool_result(result)

        try:
            from claude_agent_sdk import tool as sdk_tool
            return sdk_tool(name, description, input_schema)(wrapper)
        except ImportError:
            return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 1e. MCP server
# ---------------------------------------------------------------------------

def rewind_mcp_server(session, tools, *, name="rewind", version="0.1.0"):
    """Wrap ``create_sdk_mcp_server`` for the sandboxed tools built with ``sandbox_tool``."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError as exc:
        raise RuntimeError(
            "claude-agent-sdk is required to create a rewind MCP server."
        ) from exc

    return create_sdk_mcp_server(name=name, version=version, tools=tools)

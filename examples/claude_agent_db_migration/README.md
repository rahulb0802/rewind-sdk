# Claude Agent SDK Database Migration Demo

This example demonstrates how **Rewind** coordinates Docker/OverlayFS state rollbacks with Claude Agent SDK conversation memory during an incorrect database migration.

When an autonomous Claude agent executes an incorrect SQLite migration script or drops columns prematurely, running `verify.py` triggers an automatic rollback, restoring both the database file (`shop.db`) and the agent's message history to a clean state atomically.

This is the Claude Agent SDK counterpart to `examples/langgraph_db_migration/` — same task, same `seed_db.py`/`verify.py`, same rollback story, so the two are directly comparable side by side.

---

## Host tools vs. sandboxed tools

LangGraph tools are in-process Python functions, so `@sandbox.tool` can wrap the
function body directly and every call executes inside the checkpointed container.

The Claude Agent SDK is different: its **built-in tools run on the host**, in the
CLI subprocess — not inside the rewind container. A `Write` or `Bash` call from the
built-in toolset would mutate the host filesystem directly, and an OverlayFS
rollback would not undo it.

So this example moves execution into the sandbox via **custom SDK tools**
(`sandbox_tool` + an in-process MCP server, `rewind_mcp_server`) instead of the
built-in toolset, and uses `rewind_hooks` to:

- checkpoint before every sandboxed tool call and surface `[REWIND]` rollback
  notices back to the model after a failure (the hook equivalent of what
  `session.tool()` returns directly in the LangGraph adapter);
- **deny** the host-side `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, and `Bash`
  builtins outright, with a denial reason that steers the model toward the
  sandboxed `mcp__rewind__*` tools instead.

The result is the same guarantee a LangGraph user gets: every mutating action is
checkpointed and genuinely reversible.

---

## Prerequisites

- **Python 3.10+**
- **Docker** installed and running on your host machine
- Claude Agent SDK auth: an `ANTHROPIC_API_KEY`, or a Claude Code OAuth token
  (`CLAUDE_CODE_OAUTH_TOKEN`)

---

## Quickstart Instructions

### 1. Install dependencies

```bash
pip install --break-system-packages -e ".[claude-agent]"
```

### 2. Set Up Environment Variables

Copy the example environment template and add your credentials:

```bash
cp .env.example .env
```

Open `.env` in your text editor and add your API key:
```
ANTHROPIC_API_KEY=...
```

### 3. Seed the Workspace Database

Before running the main agent loop, initialize the baseline `shop.db` database inside `seed_workspace/`:
```bash
cd seed_workspace
python3 seed_db.py
cd ..
```
This creates `shop.db` containing sample user credit balances and legacy schemas.

### 4. Run the Agent Demo

Execute the main agent script from this directory:
```bash
python main.py
```
To override the model:
```bash
python main.py --model claude-sonnet-5
```

## What Happens During Execution?

1. The script copies `seed_workspace/` into an isolated runtime folder (`live_workspace/`) attached to the Rewind session.
2. A Claude Agent SDK agent reads `shop.db` via the sandboxed `run_sql` tool, drafts a migration script (`write_file`), executes it (`run_script`) converting `credits` to `balance_usd`, and drops the `credits` column — all inside the checkpointed container, never on the host.
3. The agent invokes `run_verify`, which executes `verify.py` inside the container sandbox.
4. If verification fails or emits an unknown status, Rewind restores `shop.db` back to its initial hash and clears the agent's conversation history in <20ms, preventing the agent from reasoning over invalid state.
5. If the agent instead tries a host-side `Write` or `Bash` call, `rewind_hooks` denies it and tells the model to use the sandboxed tools instead — that denial is visible in the transcript.

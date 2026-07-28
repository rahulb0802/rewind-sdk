"""
Rewind SDK demo — Claude Agent SDK edition
A real Claude Agent SDK agent migrates a SQLite table, converting legacy `credits` to
a tiered `balance_usd`, then drops the original column.

If the tier boundaries are wrong, the bug is invisible until verify.py
checks it, and by then `credits` is gone.
Rewind restores the database file and the agent's message history together.

verify.py emits structured JSON (pass / fail / unknown).  Auto-rollback runs
verify.py in-container via run_tests(); JSON stdout is the authoritative signal.
PASS skips rollback, FAIL rolls back, and UNKNOWN halts the session (agent mode).
Events are recorded in the ledger.

Unlike the LangGraph example, tools here execute on the *host* by default
(the Agent SDK's built-in Write/Edit/Bash run in the CLI subprocess, not the
rewind container). So this example wires everything through `sandbox_tool` +
an in-process MCP server instead: every mutating action actually runs inside
the checkpointed container, and `rewind_hooks` denies the host-side
Write/Edit/MultiEdit/NotebookEdit/Bash builtins outright, steering the model
toward the sandboxed tools instead.
"""

import argparse
import asyncio
import os
import shutil
import sys

import dotenv

dotenv.load_dotenv()

from claude_agent_sdk import ClaudeAgentOptions, query

from rewind_sdk import (
    Verifier,
    VerificationHaltError,
    rewind_hooks,
    rewind_mcp_server,
    sandbox_tool,
    session,
    track_stream,
)

DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL") or None
SEED_DIR = os.path.join(os.path.dirname(__file__), "seed_workspace")
WORK_DIR = os.path.join(os.path.dirname(__file__), "live_workspace")
MAX_WRITE_BYTES = 4_000  # cap agent-authored files

# tier cutoffs left vague on purpose, so wrong migration still "looks" done
TASK_PROMPT = """\
You have a SQLite database shop.db with a users table containing id, email,
and credits (legacy loyalty points).

We're sunsetting loyalty points in favor of cash balances in balance_usd.

Three loyalty bands were approved:
  • entry: 5 cents per point for smaller balances
  • standard: 10 cents per point for mid-range members
  • premium: 15 cents per point for our best customers

Roughly: entry is under 100 points, standard goes up through 500,
premium is 500 and above. Exact cutoff handling at 100 and 500 was
left to engineering.

Write migration.py, run it, drop credits when done, then run_verify.

You have run_sql if you want to look at the data first. Only execute one action at a time.
"""


def banner(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def print_ledger(sandbox):
    entries = sandbox.ledger.history()
    if not entries:
        print("[rewind] verification ledger: (empty)")
        return
    print("[rewind] verification ledger:")
    for entry in entries:
        print(
            f"  {entry.timestamp}  {entry.event_type:12s}  "
            f"status={entry.status or '-':7s}  "
            f"resolution={entry.resolution or '-':8s}  "
            f"checkpoint={entry.checkpoint or '-'}"
        )
        if entry.notes:
            print(f"    notes: {entry.notes}")


def _print_message(msg):
    kind = type(msg).__name__
    if kind == "AssistantMessage":
        for block in getattr(msg, "content", None) or []:
            bkind = type(block).__name__
            if bkind == "TextBlock":
                text = getattr(block, "text", "")
                if text:
                    print(f"\n[assistant] {text[:700]}")
            elif bkind == "ToolUseBlock":
                print(f"  -> {block.name}({str(block.input)[:140]})")
    elif kind == "ResultMessage":
        result = getattr(msg, "result", None)
        if result:
            print(f"\n[result] {str(result)[:700]}")


async def run_agent(sandbox, opts):
    async for msg in track_stream(sandbox, query(prompt=TASK_PROMPT, options=opts)):
        _print_message(msg)


def main(model: str | None = DEFAULT_MODEL):
    # reset workspace from seed
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.copytree(SEED_DIR, WORK_DIR, dirs_exist_ok=True)

    banner("REWIND DEMO -- credits to balance_usd migration (Claude Agent SDK)")
    sandbox = session("rewind_demo", workspace=WORK_DIR, mode="agent", auto_commit=True)  # unknown to halt
    try:
        with sandbox:
            sandbox.auto_checkpoint(trigger="before_tool_call", keep_last=20)
            # verify.py fail/exception, rollback to pre_migration
            sandbox.auto_rollback(
                "exception",
                "test_failure",
                to="pre_migration",
                verifier=Verifier(
                    command="python3 verify.py",
                    retries=2,
                    retry_delay=1.0,
                    timeout=30.0,
                ),
            )

            print(f"[rewind] sandbox: {WORK_DIR}")
            print("[rewind] auto_checkpoint: before every tool call")
            print("[rewind] auto_rollback: test_failure | exception -> pre_migration")
            print("[rewind] verifier: in-container verify.py JSON (pass/fail/unknown) + ledger")

            # named restore point; auto_rollback targets this
            sandbox.checkpoint("pre_migration", messages=[
                # msgs snapshotted too. Rollback restores agent context
                {"role": "system", "content": "Baseline: shop.db seeded, credits intact, no migration run."}
            ])

            pre = sandbox.run(
                "python3 -c \"import sqlite3; c=sqlite3.connect('shop.db').cursor();"
                "c.execute('SELECT * FROM users'); [print(r) for r in c.fetchall()]\""
            )
            print(f"\n[pre-migration data]\n{pre}")

            def show_evidence(label):
                # hit db via sandbox.run, not agent tools
                print(f"\n{'─'*70}")
                print(f"EVIDENCE — {label}")
                print(f"{'─'*70}")
                cols = sandbox.run(
                    "python3 -c \"import sqlite3; c=sqlite3.connect('shop.db').cursor();"
                    "c.execute('PRAGMA table_info(users)');"
                    "print('columns:', [r[1] for r in c.fetchall()])\""
                )
                print(cols)
                rows = sandbox.run(
                    "python3 -c \"import sqlite3; c=sqlite3.connect('shop.db').cursor();"
                    f"c.execute('SELECT * FROM users');"
                    "[print(r) for r in c.fetchall()]\""
                )
                print(rows)
                h = sandbox.run("sha256sum shop.db")  # disk fingerprint
                print(f"hash: {h}")
                return h

            show_evidence("BEFORE migration (original data)")

            @sandbox_tool(sandbox, "run_sql", "Run a read-only SQL query against shop.db.",
                          {"query": str}, rollback_on_error=False)  # reads shouldn't roll back
            async def run_sql(args):
                query_str = args["query"]
                inline_cmd = (
                    "python3 -c \"import sqlite3; "
                    "conn = sqlite3.connect('shop.db'); "
                    f"cur = conn.cursor(); cur.execute({repr(query_str)}); "
                    "[print(r) for r in cur.fetchall()]\""
                )
                return sandbox.run(inline_cmd) or "(no output)"

            @sandbox_tool(sandbox, "write_file", "Write a file (e.g. migration.py) to the workspace. Keep under 4KB.",
                          {"path": str, "content": str})
            async def write_file(args):
                path, content = args["path"], args["content"]
                if len(content.encode()) > MAX_WRITE_BYTES:
                    return f"ERROR: {len(content.encode())} bytes exceeds {MAX_WRITE_BYTES} byte limit."
                sandbox.write_file(path, content)
                return f"wrote {path} ({len(content.encode())} bytes)"

            @sandbox_tool(sandbox, "run_script", "Execute a Python script already written to the workspace.",
                          {"filename": str})
            async def run_script(args):
                filename = args["filename"]
                out = sandbox.run(f"python3 {filename}") or "(no output)"
                show_evidence(f"AFTER running {filename} (before verification)")  # demo trail
                return out

            @sandbox_tool(sandbox, "run_verify", "Check whether the migration is correct. Failure triggers automatic rollback.",
                          {})
            async def run_verify(args):
                return sandbox.run_tests()  # JSON pass/fail/unknown

            mcp_server = rewind_mcp_server(sandbox, [run_sql, write_file, run_script, run_verify])
            opts = ClaudeAgentOptions(
                system_prompt="You are a careful but autonomous database migration agent.",
                mcp_servers={"rewind": mcp_server},
                hooks=rewind_hooks(sandbox),  # denies host Write/Edit/MultiEdit/NotebookEdit/Bash
                allowed_tools=[
                    "mcp__rewind__run_sql",
                    "mcp__rewind__write_file",
                    "mcp__rewind__run_script",
                    "mcp__rewind__run_verify",
                ],
                cwd=WORK_DIR,
                model=model,
                max_turns=30,
            )

            banner(f"AGENT RUN -- Claude Agent SDK ({model or 'default model'})")
            try:
                asyncio.run(run_agent(sandbox, opts))
            except Exception as e:
                print(f"Error: {e}")

            banner("FINAL STATE")
            print("[rewind] checkpoints:", sandbox.engine.checkpoint_history)
            print("[rewind] last_auto_rollback:", sandbox.last_auto_rollback)
            print_ledger(sandbox)
            try:
                # final verify even if agent never called run_verify
                out = sandbox.run_tests()
                print(f"\n[verify.py]\n{out}")
            except RuntimeError as exc:
                print(f"\n[verify.py FAILED]\n{exc}")
    except VerificationHaltError as exc:  # verifier returned unknown
        banner("EXECUTION HALTED")
        print(exc)
        print("\nSandbox container is still alive for manual inspection.")
        print_ledger(sandbox)
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rewind SDK migration demo (Claude Agent SDK)")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Claude model override (default: CLAUDE_MODEL env or the Agent SDK's default)",
    )
    args = parser.parse_args()
    main(model=args.model)

"""
Rewind SDK demo
A real LangGraph agent migrates a SQLite table, converting legacy `credits` to a tiered
`balance_usd`, then drops the original column.

If the tier boundaries are wrong, the bug is invisible until verify.py
checks it, and by then `credits` is gone.
Rewind restores the database file and the agent's message history together.

verify.py emits structured JSON (pass / fail / unknown).  Auto-rollback runs
verify.py in-container via run_tests(); JSON stdout is the authoritative signal.
PASS skips rollback, FAIL rolls back, and UNKNOWN halts the session (agent mode).
Events are recorded in the ledger.

Tools use @sandbox.tool, and auto-checkpoint and error/rollback handling are
injected by the decorator (read-only run_sql sets rollback_on_error=False).
"""

import argparse
import os
import shutil
import sys
import time

import dotenv

dotenv.load_dotenv()

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from rewind_sdk import Verifier, VerificationHaltError, session, wrap_langgraph

import warnings
warnings.filterwarnings("ignore", message=".*create_react_agent.*")

DEFAULT_PROVIDER = os.environ.get("LLM_PROVIDER", "groq")
MODELS = {
    "groq": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "gemini": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
}
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


def make_llm(provider: str):
    """Return (llm, model_name, provider_label) for the chosen backend."""
    provider = provider.lower()
    if provider == "groq":
        if not os.environ.get("GROQ_API_KEY"):
            sys.exit("Set GROQ_API_KEY for Groq.")
        from langchain_groq import ChatGroq

        model = MODELS["groq"]
        return ChatGroq(model=model, temperature=0), model, "Groq"

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit("Set GEMINI_API_KEY for Gemini.")
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            sys.exit("Gemini requires: pip install langchain-google-genai")

        model = MODELS["gemini"]
        return (
            ChatGoogleGenerativeAI(model=model, temperature=0, google_api_key=api_key),
            model,
            "Gemini",
        )

    sys.exit(f"Unknown provider {provider!r}. Use 'groq' or 'gemini'.")


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


def main(provider: str = DEFAULT_PROVIDER):
    llm, model, provider_label = make_llm(provider)

    # reset workspace from seed
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.copytree(SEED_DIR, WORK_DIR, dirs_exist_ok=True)

    banner("REWIND DEMO -- credits to balance_usd migration")
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
            @sandbox.tool(rollback_on_error=False)  # reads shouldn't roll back
            def run_sql(query: str) -> str:
                """Run a read-only SQL query against shop.db."""
                inline_cmd = (
                    "python3 -c \"import sqlite3; "
                    "conn = sqlite3.connect('shop.db'); "
                    f"cur = conn.cursor(); cur.execute({repr(query)}); "
                    "[print(r) for r in cur.fetchall()]\""
                )
                return sandbox.run(inline_cmd) or "(no output)"

            @sandbox.tool
            def write_file(path: str, content: str) -> str:
                """Write a file (e.g. migration.py) to the workspace. Keep under 4KB."""
                if len(content.encode()) > MAX_WRITE_BYTES:
                    return f"ERROR: {len(content.encode())} bytes exceeds {MAX_WRITE_BYTES} byte limit."
                sandbox.write_file(path, content)
                return f"wrote {path} ({len(content.encode())} bytes)"

            @sandbox.tool
            def run_script(filename: str) -> str:
                """Execute a Python script already written to the workspace."""
                out = sandbox.run(f"python3 {filename}") or "(no output)"
                show_evidence(f"AFTER running {filename} (before verification)")  # demo trail
                return out

            @sandbox.tool
            def run_verify() -> str:
                """Check whether the migration is correct. Failure triggers automatic rollback."""
                return sandbox.run_tests()  # JSON pass/fail/unknown

            agent = create_react_agent(llm, tools=[run_sql, write_file, run_script, run_verify])
            safe_agent = wrap_langgraph(agent, session=sandbox)  # tools run inside sandbox

            banner(f"AGENT RUN -- {model} via {provider_label}")
            messages = [
                SystemMessage(content="You are a careful but autonomous database migration agent."),
                HumanMessage(content=TASK_PROMPT),
            ]
            try:
                for event in safe_agent.stream({"messages": messages}, {"recursion_limit": 30}):  # step cap
                    for _node, payload in event.items():
                        if not isinstance(payload, dict) or "messages" not in payload:
                            continue
                        last = payload["messages"][-1]
                        role = getattr(last, "type", "?")
                        content = getattr(last, "content", "")
                        if content:
                            print(f"\n[{role}] {str(content)[:700]}")
                        for tc in getattr(last, "tool_calls", None) or []:
                            print(f"  -> {tc['name']}({str(tc['args'])[:140]})")
                    time.sleep(0.2)
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
    parser = argparse.ArgumentParser(description="Rewind SDK migration demo")
    parser.add_argument(
        "--provider",
        choices=["groq", "gemini"],
        default=DEFAULT_PROVIDER,
        help="LLM backend (default: LLM_PROVIDER env or groq)",
    )
    args = parser.parse_args()
    main(provider=args.provider)

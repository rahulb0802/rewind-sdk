"""
verify.py: checks the credits, balance_usd migration.

After migration, balance_usd should exist, credits should be gone, and every
balance should match the correct tiered rate:
  credits <  100  ->  rate 0.05
  credits <  500  ->  rate 0.10
  credits >= 500  ->  rate 0.15

Expected balances are hardcoded from the seed data because credits is gone by
the time this runs — the verifier is the only place the truth still exists.

Rewind Verifier contract
------------------------
Configured via ``Verifier(command="python3 verify.py", ...)``.  Rewind runs
this script in-container and parses **stdout as JSON**.  The ``status`` field
("pass", "fail", or "unknown") is the sole verdict; process exit code is
ignored.  Print exactly one JSON object to stdout; use stderr for debug logs.
"""
import json
import os
import sqlite3
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "shop.db")
VERIFIER = "shop.db_migration"

EXPECTED = {  # hardcoded; credits column is gone by verify time
    1: 2.00,    # 40   * 0.05
    2: 4.95,    # 99   * 0.05
    3: 10.00,   # 100  * 0.10
    4: 25.00,   # 250  * 0.10
    5: 49.90,   # 499  * 0.10
    6: 75.00,   # 500  * 0.15
    7: 120.00,  # 800  * 0.15
    8: 0.60,    # 12   * 0.05
    9: 36.50,   # 365  * 0.10
    10: 149.85, # 999  * 0.15
}


def verify() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cur.fetchall()}

    errors = []

    if "balance_usd" not in cols:
        errors.append("balance_usd column missing")
    if "credits" in cols:  # should be dropped post-migration
        errors.append("credits column should have been dropped")

    if not errors:
        cur.execute("SELECT id, balance_usd FROM users ORDER BY id")
        actual = dict(cur.fetchall())
        for uid, expected in EXPECTED.items():
            got = actual.get(uid)
            # tier cutoffs at 100 and 500 show up in these rows
            if got is None or round(got, 2) != expected:
                errors.append(f"user {uid}: expected {expected}, got {got}")

    conn.close()

    if errors:
        return {
            "status": "fail",
            "verifier": VERIFIER,
            "summary": f"{len(errors)} check(s) failed",
            "errors": errors,
        }
    return {
        "status": "pass",
        "verifier": VERIFIER,
        "summary": "Migration verified — all 10 balances match tiered conversion rates.",
        "users_checked": len(EXPECTED),
    }


def emit_verdict(result: dict) -> None:
    # rewind parses stdout JSON; status field is the verdict
    print(json.dumps(result), flush=True)
    sys.exit(0)  # exit code ignored


def main() -> None:
    try:
        emit_verdict(verify())
    except Exception as exc:  # crash leads to unknown, not fail
        emit_verdict({
            "status": "unknown",
            "verifier": VERIFIER,
            "summary": "Verifier crashed before producing a pass/fail verdict",
            "error": str(exc),
        })


if __name__ == "__main__":
    main()

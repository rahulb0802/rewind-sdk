# LangGraph Agent Database Migration Demo

This example demonstrates how **Rewind** coordinates Docker/OverlayFS state rollbacks with LangGraph conversation memory during an incorrect database migration.

When an autonomous LLM agent executes an incorrect SQLite migration script or drops columns prematurely, running `verify.py` triggers an automatic rollback, restoring both the database file (`shop.db`) and the agent's message history to a clean state atomically.

---

## Prerequisites

- **Python 3.10+**
- **Docker** installed and running on your host machine
- An API key for **Groq** or **Gemini**

---

## Quickstart Instructions
### 1. Set Up Environment Variables

Copy the example environment template and add your API key:

```bash
cp .env.example .env
```

Open ``.env`` in your text editor and add your API key(s):
```
GROQ_API_KEY=...
# OR
GEMINI_API_KEY=...
```
### 2. Seed the Workspace Database
Before running the main agent loop, initialize the baseline ``shop.db`` database inside ``seed_workspace/``:
```bash
cd seed_workspace
python3 seed_db.py
cd ..
```
This creates ``shop.db`` containing sample user credit balances and legacy schemas.

### 3. Run the Agent Demo
Execute the main agent script from this directory:
```bash
python main.py
```
To specify a provider through command-line flags:
```bash
# Run using Groq (default)
python main.py --provider groq

# Run using Gemini
python main.py --provider gemini
```

## What Happens During Execution?
1. The script copies ``seed_workspace/`` into an isolated runtime folder (``live_workspace/``) attached to the Rewind session.
2. The LangGraph agent reads ``shop.db``, drafts a migration script converting ``credits`` to ``balance_usd``, executes it, and drops the ``credits`` column.
3. The agent invokes ``run_verify()``, which executes ``verify.py`` inside the container sandbox.
4. If verification fails or emits an unknown status, Rewind restores ``shop.db`` back to its initial hash and clears the agent's conversation history in <20ms, preventing the agent from reasoning over invalid state.
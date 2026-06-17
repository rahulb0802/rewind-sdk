import os
import sys
import json
import sqlite3
import subprocess
import time
from typing import Annotated, TypedDict, List, Dict, Any, Union

# LangChain / LangGraph imports
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from dotenv import load_dotenv

load_dotenv() # Load GOOGLE_API_KEY from .env

# Our Orchestrator
from rewind_orchestrator import RewindOrchestrator

# Configuration
SESSION_STATE_PATH = os.path.abspath("db_session_state.json")
SANDBOX_DIR = os.path.abspath("db_agent_sandbox")
DB_PATH = "sandbox.db" # Relative to sandbox internal root

# --- HELPERS ---

def messages_to_dicts(messages: List[BaseMessage]) -> List[Dict[str, str]]:
    """Converts LangChain message objects to the orchestrator's simple dict format."""
    dicts = []
    for m in messages:
        role = "user"
        if isinstance(m, SystemMessage): role = "system"
        elif isinstance(m, AIMessage): role = "assistant"
        elif isinstance(m, ToolMessage): role = "tool"
        dicts.append({"role": role, "content": m.content, "metadata": getattr(m, 'tool_call_id', None)})
    return dicts

def dicts_to_messages(dicts: List[Dict[str, str]]) -> List[BaseMessage]:
    """Converts orchestrator dicts back to LangChain message objects."""
    msgs = []
    for d in dicts:
        if d["role"] == "system": msgs.append(SystemMessage(content=d["content"]))
        elif d["role"] == "assistant": msgs.append(AIMessage(content=d["content"]))
        elif d["role"] == "tool": msgs.append(ToolMessage(content=d["content"], tool_call_id=d.get("metadata", "0")))
        else: msgs.append(HumanMessage(content=d["content"]))
    return msgs

# --- TOOLS ---

@tool
def deploy_database_schema() -> str:
    """Deploys a SQLite schema with users, orders, and transactions tables."""
    print("[TOOL] Deploying Database Schema...")
    sql = (
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);\n"
        "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL, "
        "FOREIGN KEY(user_id) REFERENCES users(id));\n"
        "CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, order_id INTEGER, status TEXT, "
        "FOREIGN KEY(order_id) REFERENCES orders(id));\n"
    )
    # Pipe SQL directly into sqlite3 via stdin — avoids shell quoting / newline issues
    output = orchestrator.engine._exec_docker_bin(
        ["sqlite3", f"/sandbox/workspace/{DB_PATH}"],
        input_data=sql
    )
    return f"SUCCESS: Schema deployed. Output: {output!r}"

@tool
def seed_initial_data() -> str:
    """Seeds the database with initial users and orders."""
    print("[TOOL] Seeding Initial Data...")
    sql = (
        "INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com');\n"
        "INSERT INTO users (name, email) VALUES ('Bob', 'bob@example.com');\n"
        "INSERT INTO orders (user_id, total) VALUES (1, 99.99);\n"
        "INSERT INTO orders (user_id, total) VALUES (2, 49.50);\n"
    )
    output = orchestrator.engine._exec_docker_bin(
        ["sqlite3", f"/sandbox/workspace/{DB_PATH}"],
        input_data=sql
    )
    return f"SUCCESS: Data seeded. Output: {output!r}"

@tool
def execute_sql(sql: str) -> str:
    """Executes an arbitrary SQL statement against the sandbox SQLite database.
    Use this for INSERT, UPDATE, DELETE, SELECT, or PRAGMA queries.
    Schema reminder:
      users(id, name, email)
      orders(id, user_id, total)
      transactions(id, order_id, status)
    """
    print(f"[TOOL] Executing SQL: {sql[:80]}...")
    try:
        output = orchestrator.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}"],
            input_data=sql if sql.endswith("\n") else sql + "\n"
        )
        return f"SUCCESS: SQL executed. Output: {output!r}"
    except Exception as e:
        # Return error as a string so the agent can self-correct rather than crashing
        return f"ERROR: {e}"

# Note: create_checkpoint is called programmatically in the loop for deterministic simulation
tools = [deploy_database_schema, seed_initial_data, execute_sql]
# Build a fast lookup dict to avoid StopIteration inside generators
_tool_map = {t.name: t for t in tools}

# --- AGENT SETUP ---

class AgentState(TypedDict):
    messages: List[BaseMessage]

def call_model(state: AgentState):
    print(f"DEBUG: Calling Gemini (Message Count: {len(state['messages'])})")
    response = model.bind_tools(tools).invoke(state['messages'])
    return {"messages": state['messages'] + [response]}

def run_tools(state: AgentState):
    tool_calls = state['messages'][-1].tool_calls
    msgs = []
    for t_call in tool_calls:
        t = _tool_map.get(t_call['name'])
        if t is None:
            # Return a graceful error rather than raising StopIteration inside a generator
            result = f"ERROR: Unknown tool '{t_call['name']}'. Available tools: {list(_tool_map.keys())}"
        else:
            result = t.invoke(t_call['args'])
        msgs.append(ToolMessage(content=str(result), tool_call_id=t_call['id']))
    return {"messages": state['messages'] + msgs}

def should_continue(state: AgentState):
    if state['messages'][-1].tool_calls:
        return "tools"
    return END

# Build Graph
workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", run_tools)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")
graph = workflow.compile()

# --- MAIN SCENARIO ---

orchestrator = RewindOrchestrator(SESSION_STATE_PATH, container_name="db_agent_sandbox")
model = None

def main():
    global model
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not found in environment.")
        sys.exit(1)
    
    model = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", google_api_key=api_key)

    print("\n--- PHASE 6: Gemini + LangGraph + Rewind Simulation ---\n")

    # Step 0: Init Sandbox
    print("[STEP 0] Initializing Sandbox...")
    if not os.path.exists(SANDBOX_DIR): os.makedirs(SANDBOX_DIR)
    subprocess.run([sys.executable, "rewind_cli.py", "--container-name", "db_agent_sandbox", "init", SANDBOX_DIR], capture_output=True)

    try:
        # Step 1: Successful Deployment
        print("\n[STEP 1] Agent: Deploying stable schema and data...")
        state = {"messages": [
            SystemMessage(content=(
                "You are a data engineer working with a SQLite database. "
                "The exact schema is:\n"
                "  users(id INTEGER PRIMARY KEY, name TEXT, email TEXT)\n"
                "  orders(id INTEGER PRIMARY KEY, user_id INTEGER, total REAL)\n"
                "  transactions(id INTEGER PRIMARY KEY, order_id INTEGER, status TEXT)\n"
                "Only use these exact column names. Deploy the schema and seed data."
            )),
            HumanMessage(content="Initialize the database please.")
        ]}
        state = graph.invoke(state)
        
        # Step 2: Atomic Checkpoint
        print("\n[STEP 2] Orchestrator: Creating ATOMIC CHECKPOINT 'schema_stable'...")
        history_snapshot = messages_to_dicts(state['messages'])
        orchestrator.create_atomic_checkpoint("schema_stable", history_snapshot)
        stable_msg_count = len(state['messages'])
        print(f"Checkpoint saved at message index {stable_msg_count}")

        # Step 3: The Disaster
        print("\n[STEP 3] Agent: Prompting for data corruption...")
        state['messages'].append(HumanMessage(content="Execute an unstructured raw binary insert into the 'transactions' table that breaks SQL integrity and corrupts the index. Use a single SQLite command that will make PRAGMA integrity_check fail if possible, or just create huge invalid blobs."))
        
        # We'll simulate the agent trying to fulfill this
        state = graph.invoke(state)
        
        # Verify corruption
        print("\n[V] Verifying Database Corruption...")
        integrity = orchestrator.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}", "PRAGMA integrity_check;"]
        )
        print(f"Integrity Check: {integrity}")

        # Step 4: Developer Intervention
        print("\n[STEP 4] Developer: Reviewing incident. Preparing Time-Travel Rollback...")
        time.sleep(1) # Simulation of human thinking

        # Step 5: Time-Travel Resume
        print("\n[STEP 5] TRIGGERING TIME-TRAVEL RESUME to 'schema_stable'...")
        resumed_history_dicts = orchestrator.time_travel_resume("schema_stable",
            patch_notes="Discarded corrupt binary fragments. Schema restored to stable build.")
        
        # Reconstruct LangGraph state
        state['messages'] = dicts_to_messages(resumed_history_dicts)
        
        # VERIFICATION
        print("\n--- FINAL VERIFICATION ---")
        
        # 1. DB Integrity
        print("[V] Checking Database Integrity (Filesystem layer)...")
        integrity = orchestrator.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}", "PRAGMA integrity_check;"]
        )
        if "ok" in integrity.lower():
            print("    SUCCESS: Database is clean and healthy.")
        else:
            print(f"    FAILURE: Database still corrupted: {integrity}")

        # 2. Context Truncation
        print(f"[V] Checking Message Context (Memory layer)...")
        print(f"    Current message count: {len(state['messages'])}")
        # Stable(X) + ResumeMsg(1) = X+1
        expected_count = stable_msg_count + 1
        if len(state['messages']) == expected_count:
            print(f"    SUCCESS: Memory truncated. Corrupt turns removed.")
        else:
            print(f"    FAILURE: Memory count mixup. Expected {expected_count}, got {len(state['messages'])}")

        # 3. Resume Operation
        print("\n[STEP 6] Resuming operation: Insert a valid transaction...")
        state['messages'].append(HumanMessage(content="Now that we are back on track, insert a valid 'success' transaction for order 1."))
        state = graph.invoke(state)
        
        # Final query check
        print("\n[V] Verifying Final Data State...")
        tx_data = orchestrator.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}", "SELECT * FROM transactions;"]
        )
        print(f"Transactions in DB: {tx_data}")

        print("\nPHASE 6 SIMULATION SUCCESSFUL")

    finally:
        print("\n[CLEANUP] Destroying sandbox...")
        orchestrator._run_cli(["destroy"])
        if os.path.exists(SESSION_STATE_PATH): os.remove(SESSION_STATE_PATH)

if __name__ == "__main__":
    main()

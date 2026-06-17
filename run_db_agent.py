import os
import shutil

import rewind


SANDBOX_DIR = os.path.abspath("db_agent_sandbox")
DB_PATH = "sandbox.db"


class DeterministicDbGraph:
    """Tiny graph-shaped demo object so the SDK scenario runs locally."""

    def __init__(self, session):
        self.session = session

    def invoke(self, state):
        prompt = state["messages"][-1]["content"].lower()
        messages = list(state["messages"])

        if "initialize" in prompt:
            messages.append(self._tool("deploy_database_schema", self.deploy_schema))
            messages.append(self._tool("seed_initial_data", self.seed_data))
        elif "corrupt" in prompt:
            messages.append(self._tool("corrupt_database", self.corrupt_database))
        elif "valid" in prompt and "transaction" in prompt:
            messages.append(self._tool("insert_valid_transaction", self.insert_valid_transaction))
        else:
            messages.append({"role": "assistant", "content": "No database action required."})

        return {"messages": messages}

    def _tool(self, name, fn):
        self.session.on_tool_call(messages=self.session.memory.get_messages(), tool_name=name)
        result = fn()
        return {"role": "tool", "content": result, "metadata": name}

    def deploy_schema(self):
        print("[TOOL] Deploying Database Schema...")
        sql = (
            "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);\n"
            "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL, "
            "FOREIGN KEY(user_id) REFERENCES users(id));\n"
            "CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, order_id INTEGER, status TEXT, "
            "FOREIGN KEY(order_id) REFERENCES orders(id));\n"
        )
        self._sqlite(sql)
        return "SUCCESS: Schema deployed."

    def seed_data(self):
        print("[TOOL] Seeding Initial Data...")
        sql = (
            "INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com');\n"
            "INSERT INTO users (name, email) VALUES ('Bob', 'bob@example.com');\n"
            "INSERT INTO orders (user_id, total) VALUES (1, 99.99);\n"
            "INSERT INTO orders (user_id, total) VALUES (2, 49.50);\n"
        )
        self._sqlite(sql)
        return "SUCCESS: Data seeded."

    def corrupt_database(self):
        print("[TOOL] Simulating database corruption...")
        self.session.write_file(DB_PATH, "not a sqlite database\n")
        return "CORRUPTION SIMULATED: database file overwritten."

    def insert_valid_transaction(self):
        print("[TOOL] Inserting valid transaction...")
        self._sqlite("INSERT INTO transactions (order_id, status) VALUES (1, 'success');\n")
        return "SUCCESS: Valid transaction inserted."

    def _sqlite(self, sql):
        return self.session.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}"],
            input_data=sql if sql.endswith("\n") else sql + "\n",
        )


def main():
    print("\n--- Rewind SDK Database Simulation ---\n")
    if os.path.exists(SANDBOX_DIR):
        shutil.rmtree(SANDBOX_DIR)
    os.makedirs(SANDBOX_DIR, exist_ok=True)

    with rewind.session("db_agent_sandbox", workspace=SANDBOX_DIR) as session:
        session.auto_checkpoint(trigger="before_tool_call", keep_last=3)
        session.auto_rollback(on="exception", to="latest")

        graph = rewind.wrap_langgraph(DeterministicDbGraph(session), session=session)

        print("[STEP 1] Deploying stable schema and data...")
        state = {"messages": [{"role": "user", "content": "Initialize the database please."}]}
        state = graph.invoke(state)

        print("\n[STEP 2] Creating ATOMIC CHECKPOINT 'schema_stable'...")
        session.checkpoint("schema_stable", messages=state["messages"])
        stable_msg_count = len(state["messages"])
        print(f"Checkpoint saved at message index {stable_msg_count}")

        print("\n[STEP 3] Simulating a bad agent action...")
        state["messages"].append(
            {"role": "user", "content": "Corrupt the database with an unsafe binary write."}
        )
        state = graph.invoke(state)

        print("\n[STEP 4] Triggering time-travel rollback...")
        resumed_messages = session.rollback(
            "schema_stable",
            patch_notes="Discarded corrupt binary fragments. Schema restored to stable build.",
        )
        state = {"messages": resumed_messages}

        print("\n--- FINAL VERIFICATION ---")
        integrity = session.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}", "PRAGMA integrity_check;"]
        )
        print(f"[V] Integrity Check: {integrity}")
        if "ok" not in integrity.lower():
            raise RuntimeError(f"Database still corrupted: {integrity}")

        expected_count = stable_msg_count + 1
        actual_count = len(state["messages"])
        print(f"[V] Current message count: {actual_count}")
        if actual_count != expected_count:
            raise RuntimeError(f"Memory count mismatch. Expected {expected_count}, got {actual_count}")

        print("\n[STEP 5] Resuming with a valid transaction...")
        state["messages"].append(
            {"role": "user", "content": "Now insert a valid success transaction for order 1."}
        )
        state = graph.invoke(state)

        tx_data = session.engine._exec_docker_bin(
            ["sqlite3", f"/sandbox/workspace/{DB_PATH}", "SELECT * FROM transactions;"]
        )
        print(f"[V] Transactions in DB: {tx_data}")
        if "1|1|success" not in tx_data:
            raise RuntimeError("Valid transaction was not inserted.")

        print("\nPHASE 6 SIMULATION SUCCESSFUL")


if __name__ == "__main__":
    main()

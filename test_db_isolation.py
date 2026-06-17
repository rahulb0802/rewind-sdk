import os
import shutil
import sqlite3
from rewind_core import SandboxEngine

def main():
    engine = SandboxEngine(container_name="db_sandbox")
    base_dir = os.path.abspath("test_db_base")
    
    # 1. Setup Host Database
    if os.path.exists(base_dir): 
        try:
            shutil.rmtree(base_dir)
        except:
            pass
    os.makedirs(base_dir, exist_ok=True)
    db_path = os.path.join(base_dir, "app.db")
    
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, balance INTEGER)")
    conn.execute("INSERT INTO users (name, balance) VALUES ('Alice', 1000), ('Bob', 500)")
    conn.commit()
    conn.close()
    
    print(f"[1] Host DB created at {db_path}")

    try:
        # 2. Init Sandbox & Install SQLite3 inside container
        engine.init_sandbox(base_dir)
        print("[2] Sandbox initialized. Installing sqlite inside container...")
        engine.run_cmd("apk add --no-cache sqlite")

        # 3. Create 'Healthy' Checkpoint
        engine.create_checkpoint("healthy_state")
        print("[3] Checkpoint 'healthy_state' created")

        # 4. Simulate Malicious/Accidental Corruption inside Sandbox
        # We wipe everyone's balance and delete the Bob user
        print("[4] Simulating data corruption inside sandbox...")
        engine.run_cmd("sqlite3 app.db \"UPDATE users SET balance = 0;\"")
        engine.run_cmd("sqlite3 app.db \"DELETE FROM users WHERE name = 'Bob';\"")
        
        # Verify corruption in sandbox
        corruption_check = engine.run_cmd("sqlite3 app.db 'SELECT SUM(balance) FROM users;'")
        print(f"    Sandbox balance sum: {corruption_check} (EXPECTED: 0)")
        
        # 5. Verify Host is still safe (Isolation proof)
        conn = sqlite3.connect(db_path)
        host_balance = conn.execute("SELECT SUM(balance) FROM users").fetchone()[0]
        conn.close()
        print(f"[5] Host DB balance verify: {host_balance} (EXPECTED: 1500)")
        assert host_balance == 1500, "CRITICAL: Host DB was affected by sandbox!"

        # 6. Rollback to Healthy State
        print("[6] Rolling back to 'healthy_state'...")
        engine.rollback_to_checkpoint("healthy_state")
        
        # 7. Verify Restoration
        restored_data = engine.run_cmd("sqlite3 app.db 'SELECT name, balance FROM users ORDER BY name;'")
        print(f"[7] Restored data inside sandbox:\n{restored_data}")
        assert "Alice|1000" in restored_data and "Bob|500" in restored_data, "Restoration Failed!"
        
        print("\nSUCCESS: Binary SQLite state was perfectly restored via Layered Storage.")

    finally:
        engine.destroy_sandbox()
        if os.path.exists(base_dir): 
            try:
                shutil.rmtree(base_dir)
            except:
                pass

if __name__ == "__main__":
    main()

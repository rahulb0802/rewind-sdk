"""Legacy reference sequence for the pre-SDK MCP-style workflow."""

import subprocess
import os
import shutil
import json

def agent_invoke(operation, args_list):
    """Mocks how an MCP Agent would invoke the server tools."""
    cmd = ["python", "rewind_cli.py", "--json", "--quiet", operation] + args_list
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res

def main():
    base_dir = os.path.abspath("test_mcp_mock_base")
    if os.path.exists(base_dir):
        shutil.rmtree(base_dir)
    os.makedirs(base_dir, exist_ok=True)

    try:
        # Pre-requisite initialization
        agent_invoke("init", [base_dir])
        
        print("\n--- MOCK AGENT SESSION ---")

        # 1. Take a checkpoint
        print("AGENT: Taking checkpoint 'pre_risky_change'")
        res = agent_invoke("checkpoint", ["pre_risky_change"])
        assert res.returncode == 0, f"Checkpoint failed: {res.stderr}"

        # 2. Execute risky change
        print("AGENT: Executing risky change (creating 'virus.exe')")
        agent_invoke("exec", ["echo 'malicious code' > virus.exe"])
        
        # 3. Verify execution success
        print("AGENT: Verifying virus was created")
        res = agent_invoke("exec", ["ls virus.exe"])
        assert "virus.exe" in res.stdout, "Verification failed, file was not created!"

        # 4. Agent realizes mistake and rolls back
        print("AGENT: Oops! Tests failed. Triggering Rollback to 'pre_risky_change'")
        res = agent_invoke("rollback", ["pre_risky_change"])
        assert res.returncode == 0, f"Rollback failed: {res.stderr}"

        # 5. Final self-correction verify
        print("AGENT: Checking if sandbox is clean...")
        res = agent_invoke("exec", ["cat virus.exe"])
        # Expecting grep/cat to fail since file is gone
        assert "No such file or directory" in res.stdout or res.returncode != 0
        
        print("AGENT SESSION SUCCESS: The agent successfully self-healed using the Sandbox Tools!")

    finally:
        agent_invoke("destroy", [])
        if os.path.exists(base_dir):
            shutil.rmtree(base_dir)

if __name__ == "__main__":
    main()

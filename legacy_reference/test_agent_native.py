"""Legacy reference sequence for the pre-SDK CLI behavior."""

import subprocess
import os
import shutil
import json
import sys

def run_cli(args, container_name="agent_test_box"):
    cmd = ["python", "rewind_cli.py", "--container-name", container_name] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res

def main():
    base_dir = os.path.abspath("test_agent_base")
    if os.path.exists(base_dir):
        shutil.rmtree(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    
    with open(os.path.join(base_dir, "test.txt"), "w") as f:
        f.write("data")

    try:
        print("[1] Testing Idempotency (init)")
        res1 = run_cli(["init", base_dir])
        assert res1.returncode == 0
        
        # Second init should return status/success without error
        res2 = run_cli(["init", base_dir])
        assert res2.returncode == 0
        assert "already exists" in res2.stderr
        print("    SUCCESS: Init is idempotent.")

        print("[2] Testing JSON + Quiet Mode")
        # Should produce exactly one line of JSON on stdout, nothing on stderr
        res3 = run_cli(["--json", "--quiet", "status"])
        assert res3.returncode == 0
        assert res3.stderr == ""
        data = json.loads(res3.stdout)
        assert data["success"] is True
        assert "container" in data["data"]
        print("    SUCCESS: JSON and Quiet mode verified.")

        print("[3] Testing Error Codes (Invalid Rollback)")
        # Code 3 is EXIT_ERROR_INVALID_CHECKPOINT
        res4 = run_cli(["rollback", "nonexistent_label"])
        assert res4.returncode == 3
        print("    SUCCESS: Exit code 3 returned for invalid checkpoint.")

        print("[4] Testing Error Codes (Container Not Found)")
        # Code 2 is EXIT_ERROR_CONTAINER_NOT_FOUND
        res5 = run_cli(["--container-name", "missing_box", "status"])
        assert res5.returncode == 2
        print("    SUCCESS: Exit code 2 returned for missing container.")

        print("[5] Testing Exec (JSON)")
        res6 = run_cli(["--json", "exec", "cat test.txt"])
        assert res6.returncode == 0
        data = json.loads(res6.stdout)
        assert data["data"] == "data"
        print("    SUCCESS: Exec returned JSON data.")

    finally:
        run_cli(["destroy"])
        if os.path.exists(base_dir):
            shutil.rmtree(base_dir)

if __name__ == "__main__":
    main()

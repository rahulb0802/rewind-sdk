"""Legacy reference sequence for the pre-SDK CLI workflow."""

import subprocess
import os
import shutil
import json

def run_cli(args):
    cmd = ["python", "rewind_cli.py"] + args
    print(f"RUNNING: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"STDOUT: {res.stdout}")
        print(f"STDERR: {res.stderr}")
    return res

def main():
    base_dir = os.path.abspath("test_cli_base")
    if os.path.exists(base_dir):
        shutil.rmtree(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    
    with open(os.path.join(base_dir, "hello.txt"), "w") as f:
        f.write("Hello")

    try:
        # 1. Init
        res = run_cli(["init", base_dir])
        assert res.returncode == 0
        assert "SUCCESS" in res.stdout
        
        # 2. Exec
        res = run_cli(["exec", "echo 'world' >> hello.txt"])
        assert res.returncode == 0
        
        # 3. Checkpoint
        res = run_cli(["checkpoint", "v1"])
        assert res.returncode == 0
        
        # 4. Status
        res = run_cli(["status", "--json"])
        assert res.returncode == 0
        status = json.loads(res.stdout)
        assert status["layers"] == 2
        assert "v1" in status["checkpoints"]
        
        # 5. Mutate
        run_cli(["exec", "rm hello.txt"])
        
        # 6. Rollback
        res = run_cli(["rollback", "v1"])
        assert res.returncode == 0
        
        # 7. Verify
        res = run_cli(["exec", "cat hello.txt"])
        assert "Hello" in res.stdout
        assert "world" in res.stdout
        
        print("\nCLI VERIFICATION SUCCESSFUL")

    finally:
        run_cli(["destroy"])
        if os.path.exists(base_dir):
            shutil.rmtree(base_dir)

if __name__ == "__main__":
    main()

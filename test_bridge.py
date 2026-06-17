import os
import shutil
import json
import subprocess
import sys
from rewind_orchestrator import RewindOrchestrator

def main():
    state_file = os.path.abspath("session_test.json")
    base_dir = os.path.abspath("bridge_test_base")
    
    if os.path.exists(state_file): os.remove(state_file)
    if os.path.exists(base_dir): shutil.rmtree(base_dir)
    os.makedirs(base_dir, exist_ok=True)

    # Use a custom container name to avoid collisions
    orchestrator = RewindOrchestrator(state_file, container_name="bridge_sandbox")
    
    try:
        print("[0] Initializing Bridge Sandbox...")
        # Standard CLI init to set up the container
        subprocess.run([sys.executable, "rewind_cli.py", "--container-name", "bridge_sandbox", "init", base_dir], capture_output=True)

        # Mocking Agent Memory
        history = [
            {"role": "system", "content": "You are a helpful assistant."},
        ]

        # Step 1-3: Success
        print("[1-3] Executing successful steps...")
        for i in range(1, 4):
            # i.txt creation
            orchestrator._run_cli(["exec", f"echo 'content {i}' > {i}.txt"])
            history.append({"role": "user", "content": f"Create file {i}"})
            history.append({"role": "assistant", "content": f"Created {i}.txt"})

        # Atomic Checkpoint
        print("[*] Creating ATOMIC CHECKPOINT 'stable_v1'...")
        orchestrator.create_atomic_checkpoint("stable_v1", history)

        # Step 4: FAILURE (Agent hallucinates/bugs out)
        # Note: We append to history but DON'T include this in the checkpoint
        print("[4] Agent bugs out: Deleting all files...")
        orchestrator._run_cli(["exec", "rm *.txt"])
        history.append({"role": "user", "content": "Cleanup please"})
        history.append({"role": "assistant", "content": "Oops, I deleted everything by mistake!"})

        # Verification of current failure state
        ls_res = orchestrator._run_cli(["exec", "ls"])
        # If ls returns nothing (empty string)
        if ls_res and isinstance(ls_res, str) and len(ls_res) > 0:
             # If it returned a success dict or something else, it might be an issue.
             # In our core, run_cmd returns stdout.strip().
             print(f"DEBUG: ls_res is '{ls_res}'")
             # It should be empty because we rm'ed everything.

        # TIME TRAVEL RESUME
        print("[*] TRIGGERING TIME TRAVEL RESUME to 'stable_v1'...")
        updated_history = orchestrator.time_travel_resume("stable_v1", "Fixed the aggressive deletion logic")

        # Verify Filesystem
        print("[V] Verifying Filesystem Restoration...")
        ls_res = orchestrator._run_cli(["exec", "ls"])
        # We expect 1.txt, 2.txt, 3.txt to be present
        # Note: the CLI returns a dict with 'data' field when --json is used.
        files_found = ls_res.get("data", "") if isinstance(ls_res, dict) else str(ls_res)
        
        if "1.txt" in files_found and "2.txt" in files_found and "3.txt" in files_found:
            print("    SUCCESS: Files 1, 2, 3 have been restored.")
        else:
            print(f"    FAILURE: Files missing! Found: {ls_res}")
            sys.exit(1)

        # Verify Memory
        print("[V] Verifying Memory Integrity...")
        # History should be: [sys, u1, a1, u2, a2, u3, a3, resume_msg]
        # The buggy u4/a4 messages should have been truncated.
        if len(updated_history) == 8:
            print("    SUCCESS: Memory truncated to 8 messages (removed bug).")
        else:
            print(f"    FAILURE: History length is {len(updated_history)}, expected 8.")
            sys.exit(1)

        last_msg = updated_history[-1]
        if "System: Environment and memory rolled back" in last_msg["content"] and "Fixed the" in last_msg["content"]:
            print("    SUCCESS: Resumption prompt injected.")
        else:
            print(f"    FAILURE: Last message content incorrect: {last_msg['content']}")
            sys.exit(1)
        
        # Check that the bug message is truly GONE
        bug_found = any("deleted everything" in m["content"] for m in updated_history)
        assert not bug_found, "The agent still remembers the bug!"

        print("\nBRAIN BRIDGE VERIFICATION SUCCESSFUL")

    finally:
        # Cleanup
        print("[*] Cleaning up...")
        orchestrator._run_cli(["destroy"])
        if os.path.exists(state_file): os.remove(state_file)
        if os.path.exists(base_dir): shutil.rmtree(base_dir)

if __name__ == "__main__":
    main()

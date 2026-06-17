import time
import os
import shutil
from rewind_core import SandboxEngine

def main():
    engine = SandboxEngine()
    
    # 1. Setup Dummy data on host
    base_dir = os.path.abspath("test_base")
    if os.path.exists(base_dir):
        try:
            shutil.rmtree(base_dir)
        except Exception:
            pass
    os.makedirs(base_dir, exist_ok=True)
    with open(os.path.join(base_dir, "original.txt"), "w") as f:
        f.write("I am the original file.")
        
    try:
        engine.init_sandbox(base_dir)
        print("[1] Sandbox initialized")
        
        # Check original
        out = engine.run_cmd("cat original.txt")
        assert out == "I am the original file.", f"Got {out}"
        
        # Mutate
        engine.run_cmd("echo 'mutated' > original.txt")
        out = engine.run_cmd("cat original.txt")
        assert out == "mutated", f"Got {out}"
        print("[2] Original file mutated in sandbox")
        
        # Create Checkpoint
        t0 = time.time()
        engine.create_checkpoint("v1")
        t1 = time.time()
        print(f"[3] Checkpoint v1 created in {(t1-t0)*1000:.2f}ms")
        
        # Further Mutate
        engine.run_cmd("echo 'mutated again' > original.txt")
        engine.run_cmd("echo 'new file' > new.txt")
        
        engine.create_checkpoint("v2")
        
        engine.run_cmd("echo 'evil changes' > original.txt")
        
        # Rollback to v1
        t0 = time.time()
        engine.rollback_to_checkpoint("v1")
        t1 = time.time()
        print(f"[4] Rollback to v1 completed in {(t1-t0)*1000:.2f}ms")
        
        # Verify
        out = engine.run_cmd("cat original.txt")
        assert out == "mutated", f"Expected 'mutated', got {out}"
        
        try:
            engine.run_cmd("ls new.txt")
            assert False, "new.txt should not exist!"
        except Exception as e:
            assert "No such file or directory" in str(e)
            
        print("[5] Reverted state successfully verified inside sandbox")
            
        # Check that original base directory is unharmed
        with open(os.path.join(base_dir, "original.txt"), "r") as f:
            host_content = f.read()
        assert host_content == "I am the original file.", "Host file was illegally mutated!"
        print("[6] Host directory integrity verified")
        
    finally:
        # Teardown
        engine.destroy_sandbox()
        print("[7] Sandbox torn down")

if __name__ == "__main__":
    main()

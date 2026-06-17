import subprocess
import os

def debug_container():
    container_name = "rewind_debug"
    base_dir = os.path.abspath("test_base")
    os.makedirs(base_dir, exist_ok=True)
    
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    
    print(f"Starting debug container with mount {base_dir} -> /base")
    subprocess.run([
        "docker", "run", "-d", "--rm", "--privileged", "--name", container_name,
        "-v", f"{base_dir}:/base:ro", "alpine", "sleep", "infinity"
    ])
    
    def run_sh(cmd):
        res = subprocess.run(["docker", "exec", container_name, "sh", "-c", cmd], capture_output=True, text=True)
        print(f"CMD: {cmd}")
        print(f"STDOUT: {res.stdout.strip()}")
        print(f"STDERR: {res.stderr.strip()}")
        print("-" * 20)

    run_sh("mount | grep /base")
    run_sh("df -T /base")
    run_sh("ls -f /base") # check for d_type
    run_sh("mkdir -p /u /w /m")
    run_sh("mount -t overlay overlay -o lowerdir=/base,upperdir=/u,workdir=/w /m")
    
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

if __name__ == "__main__":
    debug_container()

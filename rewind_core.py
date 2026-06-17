import os
import subprocess
import json

class SandboxEngine:
    def __init__(self, container_name="rewind_sandbox"):
        self.container_name = container_name
        self.reset_state()

    def reset_state(self):
        self.lowerdirs = ["/sandbox/base_native"]
        self.checkpoint_history = []

    def _exec_docker_bin(self, args_list, input_data=None):
        """Executes a binary directly inside the container without a shell."""
        cmd = ["docker", "exec"]
        if input_data is not None:
            cmd.append("-i")
        
        cmd.extend([self.container_name])
        cmd.extend(args_list)
        
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=input_data
        )
        if res.returncode != 0:
            # We don't want to log sensitive input_data in exceptions
            raise Exception(f"Docker bin failed: {' '.join(cmd)}\nStderr: {res.stderr}\nStdout: {res.stdout}")
        return res.stdout.strip()

    def _save_metadata(self):
        data = {
            "checkpoint_history": self.checkpoint_history,
            "lowerdirs": self.lowerdirs
        }
        json_str = json.dumps(data)
        # Use stdin to write metadata safely
        self._exec_docker_bin(["sh", "-c", "cat > /sandbox/metadata.json"], input_data=json_str)

    def load_metadata(self):
        try:
            raw = self._exec_docker_bin(["cat", "/sandbox/metadata.json"])
            data = json.loads(raw)
            self.checkpoint_history = data.get("checkpoint_history", [])
            self.lowerdirs = data.get("lowerdirs", ["/sandbox/base_native"])
            return True
        except Exception:
            return False

    def init_sandbox(self, base_dir_path):
        base_dir_path = os.path.abspath(base_dir_path)
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        
        res = subprocess.run([
            "docker", "run", "-d", "--rm", "--privileged", "--name", self.container_name, 
             "--tmpfs", "/sandbox:exec,mode=777",
             "-v", f"{base_dir_path}:/base:ro", "alpine", "sleep", "infinity"
        ], capture_output=True, text=True)
        
        if res.returncode != 0:
            raise Exception(f"Failed to start container: {res.stderr}")

        # Install sqlite3 — not bundled in Alpine by default
        self._exec_docker_bin(["apk", "add", "--no-cache", "sqlite"])
            
        # Series of safe binary executions
        self._exec_docker_bin(["mkdir", "-p", "/sandbox/base_native", "/sandbox/upper_current", "/sandbox/work_current", "/sandbox/workspace", "/sandbox/checkpoints"])
        # cp -a still needs a shell context for the wildcard/source-dest handling
        self._exec_docker_bin(["sh", "-c", "cp -a /base/. /sandbox/base_native/"])
        
        self.reset_state()
        self._exec_docker_bin(self._get_mount_args())
        self._save_metadata()

    def _get_mount_args(self):
        lowers = ":".join(reversed(self.lowerdirs))
        opts = f"lowerdir={lowers},upperdir=/sandbox/upper_current,workdir=/sandbox/work_current,index=off"
        return ["mount", "-t", "overlay", "overlay", "-o", opts, "/sandbox/workspace"]

    def create_checkpoint(self, label):
        if label in self.checkpoint_history:
            raise ValueError(f"Checkpoint {label} already exists.")
            
        self.checkpoint_history.append(label)
        self.lowerdirs.append(f'/sandbox/checkpoints/{label}')
        
        # Execute individual operations to prevent shell injection in paths/labels
        # We allow umount failure in case it was already unmounted
        try:
            self._exec_docker_bin(["umount", "/sandbox/workspace"])
        except:
            pass
            
        self._exec_docker_bin(["mv", "/sandbox/upper_current", f"/sandbox/checkpoints/{label}"])
        self._exec_docker_bin(["rm", "-rf", "/sandbox/work_current"])
        self._exec_docker_bin(["mkdir", "-p", "/sandbox/upper_current", "/sandbox/work_current"])
        self._exec_docker_bin(self._get_mount_args())
        self._save_metadata()

    def rollback_to_checkpoint(self, label):
        if label not in self.checkpoint_history:
            raise ValueError(f"Checkpoint {label} not found.")
            
        idx = self.checkpoint_history.index(label)
        to_discard = self.checkpoint_history[idx+1:]
        
        try:
            self._exec_docker_bin(["umount", "/sandbox/workspace"])
        except:
            pass
            
        self._exec_docker_bin(["rm", "-rf", "/sandbox/upper_current", "/sandbox/work_current"])
        
        for d in to_discard:
            self._exec_docker_bin(["rm", "-rf", f"/sandbox/checkpoints/{d}"])
            
        self.checkpoint_history = self.checkpoint_history[:idx+1]
        self.lowerdirs = ["/sandbox/base_native"] + [f"/sandbox/checkpoints/{lbl}" for lbl in self.checkpoint_history]
        
        self._exec_docker_bin(["mkdir", "-p", "/sandbox/upper_current", "/sandbox/work_current"])
        self._exec_docker_bin(self._get_mount_args())
        self._save_metadata()

    def get_status(self):
        usage_out = self._exec_docker_bin(["du", "-sh", "/sandbox"])
        usage = usage_out.split()[0]
        return {
            "container": self.container_name,
            "status": "running",
            "checkpoints": self.checkpoint_history,
            "layers": len(self.lowerdirs),
            "usage": usage
        }

    def destroy_sandbox(self):
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        self.reset_state()

    def run_cmd(self, cmd_args):
        """Accepts a string or a list of arguments for execution."""
        base_cmd = ["docker", "exec", "-w", "/sandbox/workspace", self.container_name]
        
        if isinstance(cmd_args, str):
            # Fallback for complex shell strings provided by human
            executable_cmd = base_cmd + ["sh", "-c", cmd_args]
        else:
            # Secure path for agents
            executable_cmd = base_cmd + cmd_args
            
        res = subprocess.run(executable_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise Exception(f"Command failed: {cmd_args}\nStderr: {res.stderr}\nStdout: {res.stdout}")
        return res.stdout.strip()

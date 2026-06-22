import json
import os
import posixpath
import subprocess


class SandboxEngine:
    """Owns the Docker/OverlayFS mechanics for a Rewind sandbox."""

    workspace_root = "/sandbox/workspace"

    def __init__(self, container_name="rewind_sandbox"):
        self.container_name = container_name
        self.reset_state()

    def reset_state(self):
        self.lowerdirs = ["/sandbox/base_native"]
        self.checkpoint_history = []

    def container_exists(self):
        return (
            subprocess.run(
                ["docker", "inspect", self.container_name],
                capture_output=True,
            ).returncode
            == 0
        )

    def _exec_docker_bin(self, args_list, input_data=None, strip_output=True):
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
            input=input_data,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Docker bin failed: {' '.join(cmd)}\n"
                f"Stderr: {res.stderr}\nStdout: {res.stdout}"
            )
        return res.stdout.strip() if strip_output else res.stdout

    def _save_metadata(self):
        data = {
            "checkpoint_history": self.checkpoint_history,
            "lowerdirs": self.lowerdirs,
        }
        self._exec_docker_bin(
            ["sh", "-c", "cat > /sandbox/metadata.json"],
            input_data=json.dumps(data),
        )

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

        res = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--privileged",
                "--name",
                self.container_name,
                "--tmpfs",
                "/sandbox:exec,mode=777",
                "-v",
                f"{base_dir_path}:/base:ro",
                "alpine",
                "sleep",
                "infinity",
            ],
            capture_output=True,
            text=True,
        )

        if res.returncode != 0:
            raise RuntimeError(f"Failed to start container: {res.stderr}")

        self._exec_docker_bin(["apk", "add", "--no-cache", "python3", "py3-pip"])
        self._exec_docker_bin(
            [
                "mkdir",
                "-p",
                "/sandbox/base_native",
                "/sandbox/upper_current",
                "/sandbox/work_current",
                self.workspace_root,
                "/sandbox/checkpoints",
            ]
        )
        self._exec_docker_bin(["sh", "-c", "cp -a /base/. /sandbox/base_native/"])

        self.reset_state()
        self._exec_docker_bin(self._get_mount_args())
        self._save_metadata()

    def _get_mount_args(self):
        lowers = ":".join(reversed(self.lowerdirs))
        opts = (
            f"lowerdir={lowers},upperdir=/sandbox/upper_current,"
            "workdir=/sandbox/work_current,index=off"
        )
        return ["mount", "-t", "overlay", "overlay", "-o", opts, self.workspace_root]

    def create_checkpoint(self, label):
        if label in self.checkpoint_history:
            raise ValueError(f"Checkpoint {label} already exists.")

        self.checkpoint_history.append(label)
        self.lowerdirs.append(f"/sandbox/checkpoints/{label}")

        try:
            self._exec_docker_bin(["umount", "-l", self.workspace_root])
        except Exception:
            pass

        self._exec_docker_bin(["mv", "/sandbox/upper_current", f"/sandbox/checkpoints/{label}"])
        self._exec_docker_bin(["rm", "-rf", "/sandbox/work_current"])
        self._exec_docker_bin(["mkdir", "-p", "/sandbox/upper_current", "/sandbox/work_current"])

        # print("\n" + "="*50)
        # print(f"ENGINE CHECKPOINT DIAGNOSTICS ({label})")
        # print("="*50)
        # try:
        #     print("1. Active Container Mounts:")
        #     print(self._exec_docker_bin(["mount"]))
        #     print("\n2. Contents of /sandbox:")
        #     print(self._exec_docker_bin(["ls", "-la", "/sandbox"]))
        #     print("\n3. Contents of /sandbox/checkpoints:")
        #     print(self._exec_docker_bin(["ls", "-la", "/sandbox/checkpoints"]))
        # except Exception as dbg_err:
        #     print(f"Failed to fetch diagnostics: {dbg_err}")
        # print("="*50 + "\n")

        self._exec_docker_bin(self._get_mount_args())
        self._save_metadata()

    def rollback_to_checkpoint(self, label):
        if label not in self.checkpoint_history:
            raise ValueError(f"Checkpoint {label} not found.")

        idx = self.checkpoint_history.index(label)
        to_discard = self.checkpoint_history[idx + 1 :]

        try:
            self._exec_docker_bin(["umount", "-l", self.workspace_root])
        except Exception:
            pass

        self._exec_docker_bin(["rm", "-rf", "/sandbox/upper_current", "/sandbox/work_current"])

        for checkpoint in to_discard:
            self._exec_docker_bin(["rm", "-rf", f"/sandbox/checkpoints/{checkpoint}"])

        self.checkpoint_history = self.checkpoint_history[: idx + 1]
        self.lowerdirs = ["/sandbox/base_native"] + [
            f"/sandbox/checkpoints/{checkpoint}" for checkpoint in self.checkpoint_history
        ]

        self._exec_docker_bin(["mkdir", "-p", "/sandbox/upper_current", "/sandbox/work_current"])

        # print("\n" + "="*50)
        # print(f"ENGINE ROLLBACK DIAGNOSTICS ({label})")
        # print("="*50)
        # try:
        #     print("1. Active Container Mounts:")
        #     print(self._exec_docker_bin(["mount"]))
        #     print("\n2. Contents of /sandbox:")
        #     print(self._exec_docker_bin(["ls", "-la", "/sandbox"]))
        #     print("\n3. Contents of /sandbox/checkpoints:")
        #     print(self._exec_docker_bin(["ls", "-la", "/sandbox/checkpoints"]))
        #     print("\n4. Workspace Directory Existence:")
        #     print(self._exec_docker_bin(["ls", "-la", self.workspace_root]))
        # except Exception as dbg_err:
        #     print(f"Failed to fetch diagnostics: {dbg_err}")
        # print("="*50 + "\n")

        self._exec_docker_bin(self._get_mount_args())
        self._save_metadata()

    def get_status(self):
        usage_out = self._exec_docker_bin(["du", "-sh", "/sandbox"])
        usage = usage_out.split()[0]
        return {
            "container": self.container_name,
            "status": "running",
            "checkpoints": list(self.checkpoint_history),
            "layers": len(self.lowerdirs),
            "usage": usage,
        }

    def destroy_sandbox(self):
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        self.reset_state()

    def run_cmd_capturing(self, cmd_args, timeout=None):
        """Run a command in the workspace and return (stdout, stderr, returncode) without raising."""
        base_cmd = ["docker", "exec", "-w", self.workspace_root, self.container_name]
        if isinstance(cmd_args, str):
            executable_cmd = base_cmd + ["sh", "-c", cmd_args]
        else:
            executable_cmd = base_cmd + list(cmd_args)
        res = subprocess.run(executable_cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout, res.stderr, res.returncode

    def run_cmd(self, cmd_args):
        """Accepts a shell string or an argv list for execution in the workspace."""
        stdout, stderr, returncode = self.run_cmd_capturing(cmd_args)
        if returncode != 0:
            raise RuntimeError(
                f"Command failed: {cmd_args}\nStderr: {stderr}\nStdout: {stdout}"
            )
        return stdout.strip()

    def _workspace_path(self, path):
        raw = str(path).replace("\\", "/")
        normalized = posixpath.normpath("/" + raw).lstrip("/")
        if normalized in ("", ".") or normalized.startswith("../"):
            raise ValueError(f"Invalid workspace path: {path}")
        return posixpath.join(self.workspace_root, normalized)

    def write_file(self, path, content):
        target = self._workspace_path(path)
        parent = posixpath.dirname(target)
        self._exec_docker_bin(["mkdir", "-p", parent])
        self._exec_docker_bin(["tee", target], input_data=content)
        return target

    def read_file(self, path):
        target = self._workspace_path(path)
        return self._exec_docker_bin(["cat", target], strip_output=False)
    
    def commit(self, host_workspace_path):
        """Streams the sandbox workspace as a tarball and extracts it to the host."""
        import io
        import tarfile
        import subprocess

        # 1. Package the sandbox workspace inside the container and stream it to stdout
        cmd = ["docker", "exec", self.container_name, "tar", "-C", self.workspace_root, "-cf", "-", "."]
        res = subprocess.run(cmd, capture_output=True)
        
        if res.returncode != 0:
            raise RuntimeError(f"Failed to archive sandbox files: {res.stderr.decode()}")

        # 2. Extract the tar bytes directly to the host's local workspace folder [1]
        tar_bytes = res.stdout
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
            # On modern Python, we extract safely
            tar.extractall(path=host_workspace_path)

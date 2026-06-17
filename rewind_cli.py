#!/usr/bin/env python3
import argparse
import sys
import json
import os
import subprocess
from rewind_core import SandboxEngine

# Standardized Exit Codes
EXIT_SUCCESS = 0
EXIT_ERROR_GENERIC = 1
EXIT_ERROR_CONTAINER_NOT_FOUND = 2
EXIT_ERROR_INVALID_CHECKPOINT = 3
EXIT_ERROR_DOCKER_FAILURE = 4
EXIT_ERROR_INVALID_PATH = 5

class AgentNativeCLI:
    def __init__(self, container_name, use_json=False, quiet=False):
        self.engine = SandboxEngine(container_name=container_name)
        self.use_json = use_json
        self.quiet = quiet

    def log_info(self, msg):
        """Logs informational messages to stderr."""
        if not self.quiet:
            print(f"[*] {msg}", file=sys.stderr)

    def log_result(self, data, success=True):
        """Logs the final result to stdout. Standardizes on JSON if requested."""
        if self.use_json:
            result = {"success": success, "data": data}
            print(json.dumps(result))
        else:
            if isinstance(data, dict):
                for k, v in data.items():
                    print(f"{k}: {v}")
            else:
                print(data)

    def log_error(self, msg, code=EXIT_ERROR_GENERIC):
        """Logs error messages to stderr and exits with code."""
        if self.use_json:
            print(json.dumps({"success": False, "error": msg, "code": code}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(code)

    def cmd_init(self, path, force=False):
        if not os.path.exists(path):
            self.log_error(f"Path does not exist: {path}", EXIT_ERROR_INVALID_PATH)
        
        path = os.path.abspath(path)
        
        # Idempotency check
        container_exists = subprocess.run(
            ["docker", "inspect", self.engine.container_name],
            capture_output=True
        ).returncode == 0

        if container_exists and not force:
            self.log_info(f"Sandbox '{self.engine.container_name}' already exists. Attaching...")
            if self.engine.load_metadata():
                status = self.engine.get_status()
                self.log_result(status)
                return
            else:
                self.log_info("Existing container found but metadata is missing/corrupt. Re-initializing...")

        self.log_info(f"Initializing sandbox at {path}...")
        try:
            self.engine.init_sandbox(path)
            self.log_result({"path": path, "status": "initialized"})
        except Exception as e:
            self.log_error(f"Docker failure during init: {str(e)}", EXIT_ERROR_DOCKER_FAILURE)

    def _ensure_attached(self):
        if not self.engine.load_metadata():
            self.log_error(f"No active sandbox found: {self.engine.container_name}", EXIT_ERROR_CONTAINER_NOT_FOUND)

    def cmd_exec(self, cmd):
        self._ensure_attached()
        try:
            output = self.engine.run_cmd(cmd)
            self.log_result(output)
        except Exception as e:
            self.log_error(f"Execution failed: {str(e)}", EXIT_ERROR_DOCKER_FAILURE)

    def cmd_checkpoint(self, name):
        self._ensure_attached()
        if name in self.engine.checkpoint_history:
            self.log_error(f"Checkpoint '{name}' already exists.", EXIT_ERROR_INVALID_CHECKPOINT)
        try:
            self.engine.create_checkpoint(name)
            self.log_result({"checkpoint": name, "status": "created"})
        except Exception as e:
            self.log_error(f"Checkpoint failed: {str(e)}", EXIT_ERROR_DOCKER_FAILURE)

    def cmd_rollback(self, name):
        self._ensure_attached()
        if name not in self.engine.checkpoint_history:
            self.log_error(f"Checkpoint '{name}' not found.", EXIT_ERROR_INVALID_CHECKPOINT)
        try:
            self.engine.rollback_to_checkpoint(name)
            self.log_result({"checkpoint": name, "status": "restored"})
        except Exception as e:
            self.log_error(f"Rollback failed: {str(e)}", EXIT_ERROR_DOCKER_FAILURE)

    def cmd_status(self):
        self._ensure_attached()
        try:
            status = self.engine.get_status()
            self.log_result(status)
        except Exception as e:
            self.log_error(f"Status check failed: {str(e)}", EXIT_ERROR_DOCKER_FAILURE)

    def cmd_destroy(self):
        self.log_info(f"Destroying sandbox '{self.engine.container_name}'...")
        self.engine.destroy_sandbox()
        self.log_result("SUCCESS: Sandbox destroyed")

def main():
    parser = argparse.ArgumentParser(
        description="Rewind Time-Travel Sandbox CLI (Agent-Native Edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
AGENT INTEGRATION GUIDE:
  - Always use --json for programmatic state management.
  - Use --quiet to suppress logs; stdout will contain ONLY the JSON result.
  - Check exit codes for specific failure modes:
      2: Container not found (needs init)
      3: Invalid checkpoint (history mismatch)
      4: Docker failure (environment issue)
        """
    )
    parser.add_argument("--container-name", default="rewind_sandbox", help="Target sandbox instance name")
    parser.add_argument("--json", action="store_true", help="Machine-readable raw JSON output on stdout")
    parser.add_argument("--quiet", action="store_true", help="Minimize logging to stderr")
    
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # Init
    parser_init = subparsers.add_parser("init", help="Initialize sandbox (Idempotent)")
    parser_init.add_argument("path", help="Path to base host directory")
    parser_init.add_argument("--force", action="store_true", help="Wipe and re-initialize if exists")

    # Exec
    parser_exec = subparsers.add_parser("exec", help="Run command inside workspace")
    parser_exec.add_argument("cmd", help="Shell command to execute")

    # Checkpoint
    parser_cp = subparsers.add_parser("checkpoint", help="Freeze current state")
    parser_cp.add_argument("name", help="Checkpoint label")

    # Rollback
    parser_rb = subparsers.add_parser("rollback", help="Atomic disk rollback")
    parser_rb.add_argument("name", help="Historical label to restore")

    # Status
    parser_status = subparsers.add_parser("status", help="Query layer depth and usage")

    # Destroy
    parser_destroy = subparsers.add_parser("destroy", help="Atomic cleanup")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(EXIT_SUCCESS)

    cli = AgentNativeCLI(args.container_name, use_json=args.json, quiet=args.quiet)

    if args.command == "init":
        cli.cmd_init(args.path, force=args.force)
    elif args.command == "exec":
        cli.cmd_exec(args.cmd)
    elif args.command == "checkpoint":
        cli.cmd_checkpoint(args.name)
    elif args.command == "rollback":
        cli.cmd_rollback(args.name)
    elif args.command == "status":
        cli.cmd_status()
    elif args.command == "destroy":
        cli.cmd_destroy()

if __name__ == "__main__":
    main()

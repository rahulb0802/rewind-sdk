#!/usr/bin/env python3
import sys
import os
import subprocess
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: The 'mcp' package is required. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Initialize the FastMCP server
mcp = FastMCP("Rewind Sandbox Server")

def _invoke_cli(args):
    """Thin wrapper around the Agent-Native CLI. Bubbles up exits codes and JSON data."""
    # Use sys.executable to ensure we use the same python interpreter
    cli_path = os.path.join(os.path.dirname(__file__), "rewind_cli.py")
    cmd = [sys.executable, cli_path, "--json", "--quiet"] + args
    
    print(f"DEBUG: Invoking CLI: {' '.join(cmd)}", file=sys.stderr)
    
    try:
        # Added creationflags and stdin=DEVNULL for maximum Windows stability
        res = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=60,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        
        print(f"DEBUG: CLI Returned with code {res.returncode}", file=sys.stderr)
        
        if res.returncode == 0:
            return res.stdout.strip()
        else:
            # Structured feedback for the AI on failure
            err_msg = f"COMMAND FAILED (Exit Code {res.returncode})\nOutput: {res.stdout.strip()}\nErrors: {res.stderr.strip()}"
            print(f"DEBUG: CLI Error: {err_msg}", file=sys.stderr)
            return err_msg
    except subprocess.TimeoutExpired:
        msg = "ERROR: The operation timed out after 60 seconds."
        print(f"DEBUG: {msg}", file=sys.stderr)
        return msg
    except Exception as e:
        msg = f"ERROR: Unexpected bridge failure: {str(e)}"
        print(f"DEBUG: {msg}", file=sys.stderr)
        return msg

@mcp.tool()
def init_sandbox(path: str) -> str:
    """
    Initializes a new sandbox at the specified host path. 
    This must be called once before any other tools can be used.
    Note: Initializing an existing sandbox name is safe and will return its status.
    """
    return _invoke_cli(["init", path])

@mcp.tool()
def execute_sandbox_command(cmd: str) -> str:
    """
    Executes a shell command inside the isolated, transactional sandbox workspace. 
    Use this to safely run test suites, install dependencies, or execute risky logic. 
    Note: The sandbox environment is ephemeral. Changes to system-level configurations 
    or global packages may be lost upon rollback.
    """
    return _invoke_cli(["exec", cmd])

@mcp.tool()
def create_sandbox_checkpoint(name: str) -> str:
    """
    Freezes the current state of the isolated workspace disk. 
    Always take a checkpoint BEFORE beginning a complex refactor or risky operation.
    """
    return _invoke_cli(["checkpoint", name])

@mcp.tool()
def rollback_sandbox_state(name: str) -> str:
    """
    Atomic undo. Reverts the sandbox workspace back to a historical checkpoint. 
    WARNING: This instantly discards all file changes made since the target checkpoint.
    """
    return _invoke_cli(["rollback", name])

@mcp.tool()
def get_sandbox_status() -> str:
    """
    Returns the current state of the sandbox, including disk usage, snapshot layers, 
    and available checkpoints. Use this to orient yourself.
    """
    return _invoke_cli(["status"])

if __name__ == "__main__":
    mcp.run()

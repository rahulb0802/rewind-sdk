import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any

from rewind_core import SandboxEngine

@dataclass
class CheckpointEntry:
    filesystem_label: str
    history_index: int
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SessionState:
    session_id: str
    # prompt_history stores the list of message dicts: [{"role": "user", "content": "..."}, ...]
    prompt_history: List[Dict[str, str]] = field(default_factory=list)
    # Mapping of label -> CheckpointEntry
    checkpoints: Dict[str, CheckpointEntry] = field(default_factory=dict)
    
    def save(self, path: str):
        with open(path, 'w') as f:
            json.dump(asdict(self), f, indent=2)
            
    @classmethod
    def load(cls, path: str):
        if not os.path.exists(path):
            return None
        with open(path, 'r') as f:
            data = json.load(f)
            # Reconstruct nested dataclasses
            data['checkpoints'] = {k: CheckpointEntry(**v) for k, v in data['checkpoints'].items()}
            return cls(**data)

class RewindOrchestrator:
    """
    Coordinates the 'Body' (Filesystem Engine) and 'Brain' (Agent Memory).
    Ensures that rollbacks are atomic across both data planes.
    """
    def __init__(self, state_path: str, container_name="rewind_sandbox"):
        self.state_path = state_path
        self.container_name = container_name
        self.engine = SandboxEngine(container_name=container_name)
        self.state = SessionState.load(state_path) or SessionState(session_id="default_ref")

    def _run_cli(self, args: List[str]):
        """Executes a CLI command and returns parsed JSON results."""
        cmd = [sys.executable, "rewind_cli.py", "--container-name", self.container_name, "--json", "--quiet"] + args
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise Exception(f"Orchestrator CLI failure: {res.stderr.strip()}")
        # Some commands return data, some just success
        try:
            return json.loads(res.stdout) if res.stdout.strip() else {"success": True}
        except json.JSONDecodeError:
            return {"success": True, "raw": res.stdout.strip()}

    def create_atomic_checkpoint(self, label: str, current_history: List[Dict[str, str]]):
        """
        Creates a synchronized snapshot of the filesystem and the agent's memory.
        """
        # 1. Trigger filesystem checkpoint via CLI
        self._run_cli(["checkpoint", label])
        
        # 2. Update local state
        self.state.prompt_history = current_history
        self.state.checkpoints[label] = CheckpointEntry(
            filesystem_label=label,
            history_index=len(current_history)
        )
        
        # 3. Persist registry to host disk
        self.state.save(self.state_path)
        return True

    def time_travel_resume(self, label: str, patch_notes: str = None):
        """
        Rolls back the system to a previous state and prepares the memory for resumption.
        """
        if label not in self.state.checkpoints:
            raise ValueError(f"Checkpoint '{label}' not found in the session registry.")
            
        entry = self.state.checkpoints[label]
        
        # Step A: Physicallly rollback the filesystem
        self._run_cli(["rollback", entry.filesystem_label])
        
        # Step B: Truncate the LLM memory to the point of the checkpoint
        # This removes all 'future' failed turns from the agent's context.
        self.state.prompt_history = self.state.prompt_history[:entry.history_index]
        
        # Step C: Inject the Resumption System Message
        resume_msg = f"System: Environment and memory rolled back to checkpoint [{label}]."
        if patch_notes:
            resume_msg += f" Developer Patch Applied: [{patch_notes}]."
        resume_msg += " Resume execution from this exact state."
        
        self.state.prompt_history.append({"role": "system", "content": resume_msg})
        
        # Persist updated state
        self.state.save(self.state_path)
        
        return self.state.prompt_history

# Helper for subprocess needs sys

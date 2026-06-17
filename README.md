# Rewind SDK

**Transactional runtime for AI agents.** Checkpoint execution, persist state, and rollback from failures with confidence.

> *Give an AI agent the ability to try risky things, fail safely, and resume from a clean state—with no memory of the failure.*

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/downloads/)

---

## The Problem

AI agents are powerful but fragile. When they fail—bad refactoring, corrupted state, failed tests—everything breaks. There's no rollback. No recovery. You start over, hoping it doesn't happen again.

Building agents that can operate autonomously requires **resilience**. Agents need to try risky operations, learn from failures, and recover cleanly. They need transactions.

Rewind brings ACID-like guarantees to agent execution: atomic filesystem + memory state, with instant rollback to any checkpoint.

---

## What It Does

Imagine your agent is refactoring code. It modifies files, runs tests, and the tests fail. With Rewind, your agent doesn't panic—it rolls back to the last checkpoint, tries a different approach, and continues.

```python
from rewind import session

with session("agent", workspace="./src") as sess:
    sess.checkpoint("before_refactoring")
    
    # Agent does risky work
    sess.write_file("auth.py", new_implementation)
    result = sess.run("pytest")
    
    # Tests failed? Restore everything.
    if "FAILED" in result:
        sess.rollback("before_refactoring")
```

**Both filesystem and message history are restored together.** No cleanup. No state reconciliation. Just works.

---

## Features

- **Atomic Checkpoints**: Save filesystem + message state together
- **Instant Rollback**: Restore to any checkpoint with one call
- **Auto-Checkpoints**: Automatically snapshot before tool calls
- **Auto-Rollback**: Recover from failures without explicit error handling
- **Persistent Memory**: Full conversation history preserved with state
- **Docker Sandbox**: Isolated execution environment with OverlayFS
- **Framework Agnostic**: Works with any Python agent framework (LangChain, CrewAI, LangGraph, custom)
- **Message Format Flexibility**: Support dict, LangChain, LangGraph message formats

---

## Installation

**Requirements:**
- Python 3.9+
- Docker
- pip

```bash
pip install rewind-sdk
```

**From source:**
```bash
git clone https://github.com/yourusername/rewind.git
cd rewind
pip install -e .
```

---

## Quick Start

### Basic Operations

```python
from rewind import session

with session("agent", workspace="./workspace") as sess:
    # Write files
    sess.write_file("script.py", "print('hello')")
    
    # Execute commands
    output = sess.run("python script.py")
    
    # Read files
    content = sess.read_file("script.py")
```

### Checkpoints and Rollback

```python
with session("agent", workspace="./src") as sess:
    # Save state
    sess.checkpoint("stable")
    
    # Make changes
    sess.write_file("config.py", dangerous_changes)
    sess.run("pytest")
    
    # Something went wrong? Restore.
    sess.rollback("stable")
```

### Persistent Messages

```python
with session("agent", workspace="./src") as sess:
    messages = [
        {"role": "user", "content": "Find the bug"},
        {"role": "assistant", "content": "Found it in auth.py"},
    ]
    
    # Save messages with state
    sess.sync_memory(messages)
    
    # Later, retrieve the full context
    restored = sess.get_messages()
```

---

## Automated State Management

### Auto-Checkpoints: Never Lose Progress

Automatically create snapshots before operations:

```python
with session("agent", workspace="./src") as sess:
    sess.auto_checkpoint(trigger="before_tool_call", keep_last=10)
    
    # Each agent step automatically creates a checkpoint
    agent.run_step()  # ← checkpoint created
    agent.run_step()  # ← checkpoint created
    agent.run_step()  # ← checkpoint created
```

**Parameters:**
- `trigger`: When to checkpoint (`"before_tool_call"`, `"after_tool_call"`, `"before_write"`, `"before_command"`)
- `keep_last`: Only keep N checkpoints (None = keep all)

### Auto-Rollback: Self-Healing

Automatically recover from failures:

```python
with session("agent", workspace="./src") as sess:
    sess.auto_checkpoint(trigger="before_tool_call", keep_last=10)
    sess.auto_rollback("exception", "test_failure", to="latest")
    
    # If any exception occurs, automatically rollback to last checkpoint
    # Agent continues without intervention
    agent.run_step()  # ← Exception? Auto-rollback happens
```

**Track recoveries:**
```python
if sess.last_auto_rollback:
    event = sess.last_auto_rollback['event']
    checkpoint = sess.last_auto_rollback['rollback_to']
    print(f"Recovered from {event} at {checkpoint}")
```

**Events:**
- `"exception"`: Any unhandled exception
- `"test_failure"`: Tests failed (requires `test_command`)
- `"validation_error"`: Custom validation failed
- `"timeout"`: Operation timed out

---

## Real-World Example: Autonomous Developer

```python
from rewind import session

class DeveloperAgent:
    def __init__(self, workspace):
        self.sess = session("dev", workspace=workspace)
        self.messages = []
    
    def develop_feature(self, spec):
        # Checkpoint before development
        self.sess.checkpoint("before_feature")
        
        # Generate tests
        tests = self.llm.generate_tests(spec)
        self.sess.write_file("test_new_feature.py", tests)
        self.messages.append({"role": "assistant", "content": "Tests generated"})
        
        # Generate implementation
        impl = self.llm.generate_implementation(spec)
        self.sess.write_file("new_feature.py", impl)
        self.messages.append({"role": "assistant", "content": "Implementation done"})
        
        # Save state
        self.sess.sync_memory(self.messages)
        
        # Test it
        try:
            result = self.sess.run("pytest test_new_feature.py")
            self.sess.checkpoint("feature_complete")
            print(f"✅ Feature complete: {spec}")
            return True
            
        except Exception as e:
            print(f"❌ Tests failed, trying different approach...")
            
            # Rollback and try again
            self.sess.rollback("before_feature")
            self.messages = self.sess.get_messages()
            
            # Alternative implementation
            impl_v2 = self.llm.generate_simpler_implementation(spec)
            self.sess.write_file("new_feature.py", impl_v2)
            
            # Test again
            result = self.sess.run("pytest test_new_feature.py")
            self.sess.checkpoint("feature_complete")
            print(f"✅ Feature complete with simpler approach")
            return True

# Usage
with DeveloperAgent(workspace="./myapp") as agent:
    agent.develop_feature("Add JWT authentication")
    agent.develop_feature("Add rate limiting")
    agent.develop_feature("Add request logging")
```

---

## Integration Examples

### LangChain Agent

```python
from langchain.agents import AgentExecutor, create_react_agent
from langchain_openai import ChatOpenAI
from rewind import session

with session("langchain_agent", workspace="./project") as sess:
    sess.auto_checkpoint(trigger="before_tool_call", keep_last=5)
    sess.auto_rollback("exception", to="latest")
    
    llm = ChatOpenAI(model="gpt-4")
    agent = create_react_agent(llm, tools)
    executor = AgentExecutor(agent=agent, tools=tools)
    
    result = executor.invoke({"input": "Refactor the authentication system"})
```

### CrewAI Team

```python
from crewai import Agent, Task, Crew
from rewind import session

with session("crew_team", workspace="./codebase") as sess:
    sess.auto_checkpoint(trigger="before_tool_call", keep_last=10)
    sess.auto_rollback("exception", to="latest")
    
    reviewer = Agent(role="Code Reviewer", tools=[read_file, write_file])
    refactorer = Agent(role="Refactorer", tools=[write_file, run_tests])
    
    crew = Crew(
        agents=[reviewer, refactorer],
        tasks=[
            Task(description="Review auth.py", agent=reviewer),
            Task(description="Fix issues", agent=refactorer),
        ]
    )
    
    result = crew.kickoff()
```

---

## API Reference

### Session Initialization

```python
from rewind import session

sess = session(
    name: str,                      # Session identifier
    workspace: str,                 # Working directory
    container_name: str = None,     # Docker container name (auto-generated)
    destroy_on_exit: bool = True    # Auto-cleanup
)
```

### File Operations

```python
sess.write_file(path: str, content: str)     # Write file
sess.read_file(path: str) -> str             # Read file
sess.run(command: str) -> str                # Execute command
```

### Memory Operations

```python
sess.sync_memory(messages: list, message_format="auto")  # Persist messages
sess.get_messages(message_format="auto") -> list         # Retrieve messages
```

### Checkpointing

```python
sess.checkpoint(label: str, messages=None) -> str        # Create checkpoint
sess.rollback(label: str = "latest", patch_notes=None)   # Restore state
```

### Automatic Management

```python
sess.auto_checkpoint(
    trigger: str = "before_tool_call",  # "before_tool_call", "after_tool_call", "before_write", "before_command"
    keep_last: int = None               # Keep only N checkpoints (None = keep all)
) -> RewindSession

sess.auto_rollback(
    *events: str,                       # "exception", "test_failure", etc.
    to: str = "latest",                 # Where to rollback: "latest" or "checkpoint_name"
    test_command: str = None            # For detecting test failures
) -> RewindSession
```

### Session Management

```python
sess.start(workspace=None, force=False)     # Initialize sandbox
sess.attach()                               # Load existing session
sess.destroy()                              # Cleanup
sess.status() -> dict                       # Get session info
```

### Query State

```python
sess.memory.checkpoints() -> list           # Get all checkpoint labels
sess.memory.latest_label() -> str           # Get latest checkpoint
sess.memory.get_messages() -> list          # Get all messages
```

---

## Best Practices

### ✅ Do

```python
# 1. Use context managers for automatic cleanup
with session("agent", workspace="./work") as sess:
    # ... use session ...
    pass

# 2. Checkpoint before risky operations
sess.checkpoint("before_major_change")
sess.run("destructive_operation")

# 3. Keep messages in sync
messages.append({"role": "assistant", "content": result})
sess.sync_memory(messages)

# 4. Use auto-triggers for resilience
sess.auto_checkpoint(trigger="before_tool_call", keep_last=10)
sess.auto_rollback("exception", to="latest")

# 5. Label checkpoints meaningfully
sess.checkpoint("tests_passing")
sess.checkpoint("before_refactoring")
```

### ❌ Don't

```python
# Manual cleanup - easy to leak containers
sess = session("agent")
sess.start()
# ... forgot to sess.destroy()

# Sync messages inconsistently
sess.run("command")
# ... forgot to sync_memory()

# Keep unlimited checkpoints
sess.auto_checkpoint(keep_last=None)  # Will consume lots of memory

# Vague checkpoint labels
sess.checkpoint("cp1")
sess.checkpoint("cp2")
```

---

## Configuration

### Environment Variables

```bash
REWIND_DEBUG=true                   # Enable debug logging
REWIND_WORKSPACE=/path/to/work      # Default workspace directory
```

### Python Configuration

```python
from rewind.engine import SandboxEngine

engine = SandboxEngine(container_name="my_custom_name")
sess = session("agent", engine=engine)
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Docker not running | Run `docker version` or start Docker Desktop |
| "Container already exists" | Use `sess.start(force=True)` or `sess.destroy()` first |
| High memory usage | Reduce `keep_last` in auto-checkpoint or manually clear old checkpoints |
| OverlayFS sync errors | Run `sess.destroy()` and `sess.start()` to reset |

---

## Contact

Built by a solo developer. Questions, feedback, or issues?

**Email:** hello@example.com  
**Twitter:** [@your-handle](https://twitter.com/your-handle)  

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

*Rewind: Transactional runtime for AI agents.*

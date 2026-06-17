import shutil
import subprocess

import pytest

import rewind


class FakeEngine:
    def __init__(self):
        self.checkpoint_history = []
        self.rolled_back_to = None

    def load_metadata(self):
        return True

    def run_cmd(self, cmd):
        raise RuntimeError(f"Command failed: {cmd}")

    def create_checkpoint(self, label):
        self.checkpoint_history.append(label)

    def rollback_to_checkpoint(self, label):
        self.rolled_back_to = label


def test_sync_memory_tracks_messages_without_manual_indices():
    session = rewind.RewindSession(destroy_on_exit=False)
    messages = [
        {"role": "user", "content": "Create a checkpointable state."},
        {"role": "assistant", "content": "State is ready."},
    ]

    synced = session.sync_memory(messages)

    assert synced == messages
    assert session.get_messages() == messages
    assert session.memory.get_messages() == messages


def test_declarative_triggers_checkpoint_and_rollback_without_manual_calls():
    engine = FakeEngine()
    session = rewind.RewindSession(engine=engine, destroy_on_exit=False)
    messages = [
        {"role": "user", "content": "Refactor auth."},
        {"role": "assistant", "content": "Starting."},
    ]
    failed_messages = messages + [
        {"role": "tool", "content": "pytest failed", "metadata": "run_tests"},
    ]

    session.auto_checkpoint(trigger="before_tool_call", keep_last=2)
    session.auto_rollback("test_failure", "exception", to="latest", test_command="pytest")

    session.on_tool_call(messages=messages, tool_name="read_file")
    session.on_tool_call(messages=messages, tool_name="write_file")
    session.on_tool_call(messages=messages, tool_name="run_tests")
    session.sync_memory(failed_messages)

    with pytest.raises(RuntimeError):
        session.run_tests("pytest")

    assert len(session._auto_labels) == 2
    assert engine.rolled_back_to == session._auto_labels[-1]
    assert session.last_auto_rollback["event"] == "test_failure"
    assert len(session.last_auto_rollback["messages"]) == len(messages) + 1
    assert "pytest failed" not in str(session.last_auto_rollback["messages"])


def docker_available():
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "version"],
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode == 0


@pytest.mark.skipif(not docker_available(), reason="Docker is required for Rewind integration tests")
def test_session_lifecycle_restores_filesystem_and_memory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "seed.txt").write_text("base\n", encoding="utf-8")

    messages = [
        {"role": "system", "content": "You are editing a sandboxed project."},
        {"role": "user", "content": "Create the stable config."},
        {"role": "assistant", "content": "Created config/settings.txt."},
    ]
    failed_messages = messages + [
        {"role": "user", "content": "Break the config."},
        {"role": "assistant", "content": "Overwrote it with bad data."},
    ]

    with rewind.session("rewind_pytest_session", workspace=str(workspace)) as session:
        assert session.run("cat seed.txt") == "base"

        session.write_file("config/settings.txt", "version=1\n")
        assert session.read_file("config/settings.txt") == "version=1\n"

        session.sync_memory(messages)
        session.checkpoint("stable")

        session.write_file("config/settings.txt", "corrupt=true\n")
        assert session.read_file("config/settings.txt") == "corrupt=true\n"
        session.sync_memory(failed_messages)

        resumed = session.rollback("stable", patch_notes="Rejected corrupt config write.")

        assert session.read_file("config/settings.txt") == "version=1\n"
        assert len(resumed) == len(messages) + 1
        assert not any("bad data" in message["content"] for message in resumed)
        assert "Rejected corrupt config write" in resumed[-1]["content"]

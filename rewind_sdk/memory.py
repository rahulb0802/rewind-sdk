from copy import deepcopy


class MemoryStore:
    """Framework-neutral checkpoint store for message dictionaries."""

    def __init__(self):
        self._messages = []
        self._snapshots = {}
        self._order = []

    def update(self, messages):
        self._messages = deepcopy(list(messages or []))
        return self.get_messages()

    def snapshot(self, label):
        if label in self._snapshots:
            raise ValueError(f"Checkpoint {label} already exists.")
        self._snapshots[label] = len(self._messages)
        self._order.append(label)
        return self._snapshots[label]

    def rollback(self, label, patch_notes=None):
        if label not in self._snapshots:
            raise ValueError(f"Checkpoint {label} not found in memory.")

        index = self._snapshots[label]

        # prevent dangling tool call errors
        while index > 0:
            last_msg = self._messages[index - 1]
            if last_msg.get("role") == "assistant" and last_msg.get("tool_calls"):
                index -= 1
            else:
                break

        self._messages = self._messages[:index]

        resume_msg = f"System: Environment and memory rolled back to checkpoint [{label}]."
        if patch_notes:
            resume_msg += f" Developer Patch Applied: [{patch_notes}]."
        resume_msg += " Resume execution from this exact state."
        self._messages.append({"role": "system", "content": resume_msg})

        label_index = self._order.index(label)
        discarded = self._order[label_index + 1 :]
        for discarded_label in discarded:
            self._snapshots.pop(discarded_label, None)
        self._order = self._order[: label_index + 1]

        return self.get_messages()

    def latest_label(self):
        if not self._order:
            return None
        return self._order[-1]

    def get_messages(self):
        return deepcopy(self._messages)

    def checkpoints(self):
        return list(self._order)

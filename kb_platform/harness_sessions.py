"""Per-run journal provider; durable application history belongs to its own plugin."""
from copy import deepcopy


class MemorySession:
    def __init__(self, message):
        self.start_result = {}
        self._records = [{'role': 'user', 'content': deepcopy(message)}]
        self._closed = False

    def messages(self):
        return deepcopy(self._records)

    def append(self, records):
        if self._closed:
            raise RuntimeError('SESSION_CLOSED')
        self._records.extend(deepcopy(records))

    def finish(self, result):
        self._closed = True
        self._records.clear()

    def fail(self, error):
        self._closed = True
        self._records.clear()


class MemorySessions:
    """A fresh journal per invocation, with no shared evidence or replay cache."""

    def begin(self, context, message):
        return MemorySession(message)

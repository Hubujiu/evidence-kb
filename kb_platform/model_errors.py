"""Stable model failure codes, independent of any transport SDK."""
class RemoteModelError(RuntimeError):
    def __init__(self, code: str, status: int | None = None, *, retry_after_seconds=None):
        self.code, self.status = code, status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code + (f" (HTTP {status})" if status else ""))

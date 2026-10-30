class RemoteModelError(RuntimeError):
    def __init__(self, code: str, status: int | None = None, *, retry_after_seconds=None):
        self.code, self.status = code, status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code + (f" (HTTP {status})" if status else ""))


class TerminalContractError(ValueError):
    pass


class ModelPreflightError(ValueError):

    model_sent = False

    def __init__(self, code):
        self.code = code
        super().__init__(code)

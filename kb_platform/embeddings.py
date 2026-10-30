from .model_errors import ModelPreflightError


class EmbeddingError(RuntimeError):
    pass


class EmbeddingCallError(EmbeddingError):
    def __init__(self, error):
        self.code = getattr(error, 'code', str(error))
        self.preflight_code = error.code if isinstance(error, ModelPreflightError) else None
        self.status = getattr(error, 'status', None)
        self.retry_after_seconds = getattr(error, 'retry_after_seconds', None)
        self.model_sent = getattr(error, 'model_sent', None)
        self.physical_request_id = getattr(error, 'physical_request_id', None)
        super().__init__(str(error))

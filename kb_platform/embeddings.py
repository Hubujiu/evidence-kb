import json
from pathlib import Path
import re

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


def read_embedding_capture(audit_dir, ledger, request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', request_id):
        return None
    try:
        record = json.loads((Path(audit_dir) / (request_id + '.json')).read_text('utf-8'))
        if not isinstance(record, dict):
            return None
        state = ledger.entry(request_id)
        if not state:
            return None
        return {'record': record, 'physical_status': state['status'], 'physical_usage': state['usage']}
    except (OSError, ValueError):
        return None

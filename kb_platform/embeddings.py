import json
from pathlib import Path
import re

from .model_errors import error_details


class EmbeddingError(RuntimeError):
    pass


class EmbeddingCallError(EmbeddingError):
    def __init__(self, error):
        for field, value in error_details(error).items():
            setattr(self, field, value)
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

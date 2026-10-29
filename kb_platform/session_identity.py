"""Lightweight caller identity and errors shared by services and persistence."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    principal_id: str

    def __post_init__(self):
        if not self.tenant_id or not self.principal_id:
            raise ValueError('Identity requires tenant and principal')


class PlatformError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SessionPersistenceError(RuntimeError):
    code = 'SESSION_PERSISTENCE_FAILED'

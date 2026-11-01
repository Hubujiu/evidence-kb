from dataclasses import dataclass
from hashlib import sha256
import json
import math
import secrets
import time
from typing import Protocol

from .session_identity import Actor
from .storage_sdk import StorageError


class Authentication(Protocol):
    def authenticate(self, token: str) -> Actor | None: ...


def _valid_token(token):
    return isinstance(token, str) and 32 <= len(token) <= 512 and token.isascii()


class StaticAuthentication:
    def __init__(self, identities):
        if any(not _valid_token(token) or not isinstance(actor, Actor) for token, actor in identities.items()):
            raise ValueError('AUTH_INVALID_IDENTITIES')
        self._identities = dict(identities)

    def authenticate(self, token):
        if not _valid_token(token):
            return None
        for candidate, actor in self._identities.items():
            if secrets.compare_digest(token, candidate):
                return actor
        return None


@dataclass(frozen=True)
class AuthenticationSession:
    token: str
    expires_at: float


class SessionAuthentication:
    def __init__(self, store, *, clock=time.time):
        self._store = store
        self._clock = clock

    @staticmethod
    def _key(token):
        return 'auth-session:' + sha256(token.encode('ascii')).hexdigest()

    def issue(self, actor: Actor, *, ttl_seconds: float = 3600) -> AuthenticationSession:
        if (not isinstance(actor, Actor) or not isinstance(actor.tenant_id, str)
                or not isinstance(actor.principal_id, str)):
            raise ValueError('AUTH_INVALID_ACTOR')
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= 365 * 24 * 60 * 60):
            raise ValueError('AUTH_INVALID_TTL')
        token = secrets.token_urlsafe(32)
        expires_at = self._clock() + ttl_seconds
        value = json.dumps({'version': 1, 'tenant_id': actor.tenant_id,
                            'principal_id': actor.principal_id, 'expires_at': expires_at},
                           allow_nan=False).encode('utf-8')
        self._store.put_expiring(self._key(token), value, ttl_seconds=ttl_seconds)
        return AuthenticationSession(token, expires_at)

    def authenticate(self, token):
        if not _valid_token(token):
            return None
        value = self._store.get(self._key(token))
        if value is None:
            return None
        try:
            record = json.loads(value)
            if (not isinstance(record, dict) or type(record.get('version')) is not int
                    or record['version'] != 1
                    or type(record.get('expires_at')) not in (int, float)
                    or not math.isfinite(record['expires_at'])
                    or not isinstance(record.get('tenant_id'), str)
                    or not isinstance(record.get('principal_id'), str)):
                raise ValueError('INVALID_SESSION')
            actor = Actor(record['tenant_id'], record['principal_id'])
        except (ValueError, TypeError, UnicodeError):
            raise StorageError('AUTH_SESSION_INVALID') from None
        return actor if record['expires_at'] > self._clock() else None

    def revoke(self, token):
        if not _valid_token(token):
            return False
        return self._store.delete(self._key(token))

from contextlib import contextmanager
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from .session_identity import Actor, PlatformError


class DocumentParser(Protocol):
    def __call__(self, data: bytes, filename: str, policy: dict) -> dict: ...


class OriginalStorage(Protocol):
    def put(self, tenant_id: str, data: bytes) -> tuple[str, str]: ...
    def checked_path(self, path: str, tenant_id: str, digest: str): ...


class SearchBackend(Protocol):
    def index_name(self, tenant_id: str, kb_id: str, build_id: str) -> str: ...
    def stage(self, index: str, units: list, **options): ...
    def search(self, index: str, tenant_id: str, kb_id: str, allowed: list,
               query: str, limit: int, **options) -> list: ...
    def dense(self, index: str, tenant_id: str, kb_id: str, allowed: list,
              vector: list, limit: int) -> list: ...


class PostgresRepository:
    def __init__(self, dsn, *, connect=psycopg.connect):
        self.dsn, self.connect = dsn, connect

    @contextmanager
    def transaction(self, actor: Actor, kb_id: str | None = None):
        try:
            with self.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as connection:
                connection.execute(
                    "SELECT set_config('app.tenant_id', %s, true), set_config('app.kb_id', %s, true)",
                    (actor.tenant_id, kb_id or ''))
                yield connection
        except psycopg.OperationalError as error:
            raise PlatformError('AUTHORITY_UNAVAILABLE', 'Authorization service unavailable', 503) from error

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

from ..persistence_local import local_database
from ..persistence_schema import cache_schema
from ..storage_sdk import ExpiringKeyValueStore, StorageError
from .contracts import Contract, digest


class StageKey(Contract):
    stage: str
    implementation: str
    upstream: tuple[str, ...]
    config_hash: str
    corpus_hash: str
    index_snapshot: str
    principal: str
    acl_revision: str
    model_revision: str
    sample_id: str


class StageCache:
    def __init__(self, root: Path | None = None, *, database=None, objects=None,
                 payload_cache: ExpiringKeyValueStore | None = None, payload_ttl_seconds=300):
        if (type(payload_ttl_seconds) not in (int, float) or not math.isfinite(payload_ttl_seconds)
                or not 0 < payload_ttl_seconds <= 365 * 24 * 60 * 60):
            raise ValueError('INVALID_STAGE_CACHE_PAYLOAD_TTL')
        if payload_cache is not None and (root is not None or objects is not None):
            raise ValueError('CACHE_PAYLOAD_BACKEND_CONFLICT')
        if (root is None and database is None
                or root is None and objects is None and payload_cache is None):
            raise ValueError('CACHE_STORAGE_REQUIRED')
        self.root = None if root is None else Path(root)
        self.objects, self._owned = objects, None
        self.payload_cache, self.payload_ttl_seconds = payload_cache, payload_ttl_seconds
        self.db = None if self.root is None else self.root / 'cache.sqlite3'
        if database is None:
            database, self._owned = local_database(self.db, owner='stage-cache')
        self.database = database
        if objects is None and payload_cache is None:
            (self.root / 'objects').mkdir(parents=True, exist_ok=True)
        self._key_column = '`key`' if self.database.info.engine == 'mysql' else 'key'
        try:
            self.database.prepare(cache_schema(self.database.identity.space.owner_id, self.database.info.engine))
            self.database.seal()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._owned is not None:
            self._owned.close()

    def put(self, key: StageKey, payload):
        content_hash = digest(payload)
        key_hash = digest(key)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if self.payload_cache is not None:
            locator = None
        elif self.objects is None:
            target = self.root / 'objects' / (content_hash + '.json')
            try:
                with target.open('x', encoding='utf-8') as handle:
                    handle.write(raw)
            except FileExistsError:
                if digest(json.loads(target.read_text('utf-8'))) != content_hash:
                    raise ValueError('cache object integrity failure')
            locator = None
        else:
            locator = self.objects.put('stage-cache', raw.encode('utf-8')).locator
        insert = ('INSERT IGNORE INTO entries(`key`,hash,object_ref) VALUES(:key,:hash,:ref)'
                  if self.database.info.engine == 'mysql' else
                  'INSERT INTO entries(key,hash,object_ref) VALUES(:key,:hash,:ref) ON CONFLICT(key) DO NOTHING')
        with self.database.transaction(write=True) as db:
            db.execute(insert, {'key': key_hash, 'hash': content_hash, 'ref': locator})
            old = db.execute('SELECT hash FROM entries WHERE ' + self._key_column + '=:key',
                             {'key': key_hash}).fetchone()
            if old['hash'] != content_hash:
                raise ValueError("stage output changed; use a new implementation/sample identity")
        self._remember(content_hash, raw.encode('utf-8'))

    def _remember(self, content_hash, raw):
        if self.payload_cache is not None:
            try:
                self.payload_cache.put_expiring(content_hash, raw, ttl_seconds=self.payload_ttl_seconds)
            except (StorageError, OSError):
                pass

    def _cached(self, content_hash):
        if self.payload_cache is not None:
            try:
                return self.payload_cache.get(content_hash)
            except (StorageError, OSError):
                pass
        return None

    def get(self, key: StageKey, *, authorize: Callable[[], bool], mode="replay", under_test=False):

        if not authorize():
            raise PermissionError("current authorization rejected cached artifact")
        if mode not in ("replay", "live", "cold"):
            raise ValueError("unknown cache mode")
        if mode == "cold" or (mode == "live" and under_test):
            return None
        with self.database.transaction(write=False) as db:
            row = db.execute('SELECT hash,object_ref FROM entries WHERE ' + self._key_column + '=:key',
                             {'key': digest(key)}).fetchone()
        if row is None:
            return None
        if self.payload_cache is not None:
            raw = self._cached(row['hash'])
            if raw is None:
                return None
            try:
                if not isinstance(raw, bytes):
                    raise ValueError('cache object integrity failure')
                payload = json.loads(raw)
                if digest(payload) != row['hash']:
                    raise ValueError('cache object integrity failure')
            except (ValueError, TypeError, UnicodeError):
                raise ValueError('cache object integrity failure') from None
            return payload
        if row['object_ref'] is None:
            if self.root is None:
                raise ValueError('CACHE_LOCAL_OBJECT_STORAGE_REQUIRED')
            raw = (self.root / 'objects' / (row['hash'] + '.json')).read_text('utf-8')
        else:
            if self.objects is None:
                raise ValueError('CACHE_BLOB_STORAGE_REQUIRED')
            raw = self.objects.read('stage-cache', row['object_ref']).decode('utf-8')
        payload = json.loads(raw)
        if digest(payload) != row['hash']:
            raise ValueError("cache object integrity failure")
        return payload

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..persistence_local import local_database
from ..persistence_schema import cache_schema
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
    def __init__(self, root: Path | None = None, *, database=None, objects=None):
        if root is None and (database is None or objects is None):
            raise ValueError('CACHE_STORAGE_REQUIRED')
        self.root = None if root is None else Path(root)
        self.objects, self._owned = objects, None
        self.db = None if self.root is None else self.root / 'cache.sqlite3'
        if database is None:
            database, self._owned = local_database(self.db, owner='stage-cache')
        self.database = database
        if objects is None:
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
        if self.objects is None:
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

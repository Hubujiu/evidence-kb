from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .contracts import digest


class ReadThroughStageCache:
    def __init__(self, current, parents, *, verify_payload):
        self.current = current
        self.parents = tuple(Path(p).resolve(strict=True) for p in parents)
        self.verify_payload = verify_payload
        self.imported = {}

    def put(self, key, payload):
        return self.current.put(key, payload)

    def get(self, key, *, authorize, mode='replay', under_test=False):
        result = self.current.get(key, authorize=authorize, mode=mode, under_test=under_test)
        if result is not None or mode == 'cold' or (mode == 'live' and under_test):
            return result
        for root in self.parents:
            with closing(sqlite3.connect((root/'cache.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
                row = db.execute('SELECT hash FROM entries WHERE key=?',(digest(key),)).fetchone()
            if row is None:
                continue
            if len(row[0]) != 64 or any(c not in '0123456789abcdef' for c in row[0]):
                raise ValueError('INVALID_PARENT_CACHE_OBJECT_HASH')
            payload = json.loads((root/'objects'/(row[0]+'.json')).read_text('utf8'))
            if digest(payload) != row[0]:
                raise ValueError('PARENT_CACHE_INTEGRITY_FAILURE')
            self.verify_payload(root,key,payload)
            if not authorize():
                raise PermissionError('CURRENT_AUTHORIZATION_REVOKED')
            self.current.put(key,payload)
            self.imported[digest(key)]={'parent':str(root),'object_sha256':row[0]}
            return self.current.get(key,authorize=authorize,mode=mode,under_test=under_test)
        return None

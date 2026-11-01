from hashlib import sha256
import json

from .database_contracts import Migration, SchemaPackage


class ImmutableJsonJournal:
    def __init__(self, database, objects, *, namespace):
        self.database, self.objects, self.namespace = database, objects, namespace
        owner = database.identity.space.owner_id
        database.prepare(SchemaPackage(owner, (Migration(1, (
            'CREATE TABLE journal_entries (namespace VARCHAR(64) NOT NULL, entry_key VARCHAR(256) NOT NULL, '
            'object_ref TEXT NOT NULL, content_sha256 VARCHAR(64) NOT NULL, PRIMARY KEY(namespace,entry_key))',
        )),), runtime_appendonly=('journal_entries',)))
        database.seal()

    def _content(self, row):
        raw = self.objects.read(self.namespace, row['object_ref'])
        if sha256(raw).hexdigest() != row['content_sha256']:
            raise ValueError('JOURNAL_CONTENT_INTEGRITY_FAILURE')
        return raw

    def read(self, key):
        with self.database.transaction(write=False) as db:
            row = db.execute('SELECT object_ref,content_sha256 FROM journal_entries '
                             'WHERE namespace=:namespace AND entry_key=:key',
                             {'namespace': self.namespace, 'key': key}).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return json.loads(self._content(row))

    def put(self, key, value):
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError('INVALID_JOURNAL_ENTRY_KEY')
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        ref = self.objects.put(self.namespace, raw)
        insert = ('INSERT IGNORE INTO journal_entries(namespace,entry_key,object_ref,content_sha256) '
                  'VALUES(:namespace,:key,:ref,:hash)' if self.database.info.engine == 'mysql' else
                  'INSERT INTO journal_entries(namespace,entry_key,object_ref,content_sha256) '
                  'VALUES(:namespace,:key,:ref,:hash) ON CONFLICT(namespace,entry_key) DO NOTHING')
        with self.database.transaction(write=True) as db:
            db.execute(insert, {'namespace': self.namespace, 'key': key, 'ref': ref.locator,
                                'hash': sha256(raw).hexdigest()})
            old = db.execute('SELECT content_sha256 FROM journal_entries WHERE namespace=:namespace AND entry_key=:key',
                             {'namespace': self.namespace, 'key': key}).fetchone()
            if old['content_sha256'] != sha256(raw).hexdigest():
                raise ValueError('PROVIDER_ADMISSION_IMMUTABLE_CONFLICT')

    def items(self, prefix):
        with self.database.transaction(write=False) as db:
            rows = db.execute('SELECT entry_key,object_ref,content_sha256 FROM journal_entries '
                              'WHERE namespace=:namespace ORDER BY entry_key',
                              {'namespace': self.namespace}).fetchall()
        return [(row['entry_key'], self._content(row)) for row in rows if row['entry_key'].startswith(prefix)]

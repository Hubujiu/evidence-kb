from hashlib import sha256
import json

from sqlalchemy import Column, MetaData, String, Table, Text, select

from .storage_sdk.relational import ExactString, StructuredSchema


metadata = MetaData()
journal_entries = Table('journal_entries', metadata,
    Column('namespace', ExactString(64), primary_key=True),
    Column('entry_key', ExactString(256), primary_key=True),
    Column('object_ref', Text, nullable=False),
    Column('content_sha256', String(64), nullable=False))


class ImmutableJsonJournal:
    def __init__(self, database, objects, *, namespace):
        self.database, self.objects, self.namespace = database, objects, namespace
        owner = database.identity.space.owner_id
        database.prepare(StructuredSchema(owner, metadata, appendonly=('journal_entries',)))
        database.seal()

    def _content(self, row):
        raw = self.objects.read(self.namespace, row['object_ref'])
        if sha256(raw).hexdigest() != row['content_sha256']:
            raise ValueError('JOURNAL_CONTENT_INTEGRITY_FAILURE')
        return raw

    def read(self, key):
        with self.database.transaction(write=False) as db:
            row = db.execute(select(journal_entries.c.object_ref, journal_entries.c.content_sha256).where(
                journal_entries.c.namespace == self.namespace, journal_entries.c.entry_key == key)).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return json.loads(self._content(row))

    def put(self, key, value):
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError('INVALID_JOURNAL_ENTRY_KEY')
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        ref = self.objects.put(self.namespace, raw)
        with self.database.transaction(write=True) as db:
            db.insert(journal_entries, {'namespace': self.namespace, 'entry_key': key, 'object_ref': ref.locator,
                'content_sha256': sha256(raw).hexdigest()}, conflict_keys=('namespace', 'entry_key'))
            old = db.execute(select(journal_entries.c.content_sha256).where(
                journal_entries.c.namespace == self.namespace, journal_entries.c.entry_key == key)).fetchone()
            if old['content_sha256'] != sha256(raw).hexdigest():
                raise ValueError('PROVIDER_ADMISSION_IMMUTABLE_CONFLICT')

    def items(self, prefix):
        with self.database.transaction(write=False) as db:
            rows = db.execute(select(journal_entries.c.entry_key, journal_entries.c.object_ref,
                journal_entries.c.content_sha256).where(journal_entries.c.namespace == self.namespace)
                .order_by(journal_entries.c.entry_key)).fetchall()
        return [(row['entry_key'], self._content(row)) for row in rows if row['entry_key'].startswith(prefix)]

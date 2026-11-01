from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json

from sqlalchemy.exc import DBAPIError, IntegrityError

from ..database_contracts import Migration, QueryLimits, SchemaPackage
from ..storage_sdk import Document, StorageError


MAX_VALUE_BYTES = 1024 * 1024
MAX_BATCH_ITEMS = 100
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_ITEMS = 10000
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_REVISION = 2**63 - 1


def validate_key(key):
    try:
        valid = isinstance(key, str) and bool(key) and '\x00' not in key and len(key.encode('utf-8')) <= 512
    except UnicodeError:
        valid = False
    if not valid:
        raise StorageError('STORAGE_INVALID_KEY')
    return key


def validate_value(value):
    if type(value) is not bytes or len(value) > MAX_VALUE_BYTES:
        raise StorageError('STORAGE_INVALID_VALUE')
    return value


def _json_value(value, depth=0):
    if depth > 64:
        raise StorageError('STORAGE_INVALID_DOCUMENT')
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise StorageError('STORAGE_INVALID_DOCUMENT')
        children = value.values()
    elif type(value) is list:
        children = value
    else:
        raise StorageError('STORAGE_INVALID_DOCUMENT')
    for child in children:
        _json_value(child, depth + 1)


def _document_json(value):
    if type(value) is not dict:
        raise StorageError('STORAGE_INVALID_DOCUMENT')
    try:
        _json_value(value)
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise StorageError('STORAGE_INVALID_DOCUMENT') from None
    if len(encoded.encode('utf-8')) > MAX_VALUE_BYTES:
        raise StorageError('STORAGE_LIMIT_EXCEEDED')
    return encoded


def _revision(value, *, optional=True):
    if value is None and optional:
        return
    if type(value) is not int or value < 0 or value > MAX_REVISION:
        raise StorageError('STORAGE_INVALID_REVISION')


def _key_hash(key):
    return sha256(validate_key(key).encode('utf-8')).hexdigest()


def _document(row, expected_key=None):
    if row is None:
        return None
    try:
        key, payload, revision = row['item_key'], row['payload'], row['revision']
        validate_key(key)
        _revision(revision, optional=False)
        value = json.loads(payload)
        if (revision < 1 or _document_json(value) != payload or
                row['key_hash'] != _key_hash(key) or expected_key is not None and key != expected_key):
            raise ValueError('INVALID_RECORD')
        return Document(key, value, revision)
    except (KeyError, TypeError, ValueError, RecursionError, StorageError):
        raise StorageError('STORAGE_DATA_CORRUPT') from None


class _SqlStore:
    def __init__(self, database, *, table, fields):
        self._database = database
        self.identity = database.identity
        self._engine = database.info.engine
        if self._engine not in {'sqlite', 'postgresql', 'mysql'}:
            raise StorageError('STORAGE_BACKEND_UNSUPPORTED')
        self._table = table
        database.prepare(SchemaPackage(self.identity.space.owner_id, (Migration(1, (
            'CREATE TABLE ' + table + '(key_hash VARCHAR(64) PRIMARY KEY,item_key TEXT NOT NULL,' + fields + ')',
        )),)))
        database.seal()

    @contextmanager
    def _transaction(self, *, write=False, limits=None):
        try:
            with self._database.transaction(write=write, limits=limits) as tx:
                yield tx
        except DBAPIError:
            raise StorageError('STORAGE_BACKEND_UNAVAILABLE') from None

    def _upsert(self, columns, updates):
        names = ','.join(columns)
        params = ','.join(':' + column for column in columns)
        sql = 'INSERT INTO ' + self._table + '(' + names + ') VALUES(' + params + ')'
        if self._engine == 'mysql':
            return sql + ' ON DUPLICATE KEY UPDATE ' + ','.join(updates)
        return sql + ' ON CONFLICT(key_hash) DO UPDATE SET ' + ','.join(updates)


class SqlDocumentStore(_SqlStore):
    def __init__(self, database):
        if database.info.engine not in {'sqlite', 'postgresql'}:
            raise StorageError('STORAGE_BACKEND_UNSUPPORTED')
        super().__init__(database, table='kb_storage_documents', fields='payload TEXT NOT NULL,revision BIGINT NOT NULL '
                         'CHECK(revision>0 AND revision<=' + str(MAX_REVISION) + '),UNIQUE(item_key)')

    def _lock_writes(self, tx):
        if self._engine == 'postgresql':
            tx.execute('SELECT pg_advisory_xact_lock(:key)', {'key': int(self.identity.space.digest[:15], 16)})

    def get(self, document_id):
        key_hash = _key_hash(document_id)
        with self._transaction() as tx:
            row = tx.execute('SELECT key_hash,item_key,payload,revision FROM kb_storage_documents WHERE key_hash=:key',
                             {'key': key_hash}).first()
            return _document(row, document_id)

    def get_many(self, document_ids):
        if not isinstance(document_ids, (tuple, list)) or len(document_ids) > MAX_BATCH_ITEMS:
            raise StorageError('STORAGE_LIMIT_EXCEEDED')
        keys = tuple(_key_hash(key) for key in document_ids)
        limits = QueryLimits(max_rows=MAX_BATCH_ITEMS, max_bytes=MAX_BATCH_BYTES, max_statements=MAX_BATCH_ITEMS)
        with self._transaction(limits=limits) as tx:
            return tuple(_document(tx.execute(
                'SELECT key_hash,item_key,payload,revision FROM kb_storage_documents WHERE key_hash=:key',
                {'key': key_hash}).first(), key) for key_hash, key in zip(keys, document_ids, strict=True))

    def put(self, document_id, value, *, expected_revision=None):
        key_hash, payload = _key_hash(document_id), _document_json(value)
        _revision(expected_revision)
        if expected_revision == MAX_REVISION:
            raise StorageError('STORAGE_REVISION_CONFLICT')
        values = {'key_hash': key_hash, 'item_key': document_id, 'payload': payload, 'revision': 1}
        try:
            with self._database.transaction(write=True) as tx:
                self._lock_writes(tx)
                if expected_revision == 0:
                    tx.execute('INSERT INTO kb_storage_documents(key_hash,item_key,payload,revision) '
                               'VALUES(:key_hash,:item_key,:payload,:revision)', values)
                elif expected_revision is not None:
                    result = tx.execute('UPDATE kb_storage_documents SET payload=:payload,revision=revision+1 '
                                        'WHERE key_hash=:key_hash AND item_key=:item_key AND revision=:expected',
                                        {**values, 'expected': expected_revision})
                    if result.rowcount != 1:
                        raise StorageError('STORAGE_REVISION_CONFLICT')
                else:
                    sql = self._upsert(('key_hash', 'item_key', 'payload', 'revision'),
                                       ('payload=excluded.payload', 'revision=kb_storage_documents.revision+1'))
                    tx.execute(sql, values)
                row = tx.execute('SELECT key_hash,item_key,payload,revision FROM kb_storage_documents WHERE key_hash=:key',
                                 {'key': key_hash}).first()
                return _document(row, document_id)
        except IntegrityError:
            raise StorageError('STORAGE_REVISION_CONFLICT') from None
        except DBAPIError:
            raise StorageError('STORAGE_BACKEND_UNAVAILABLE') from None

    def delete(self, document_id, *, expected_revision=None):
        key_hash = _key_hash(document_id)
        _revision(expected_revision)
        with self._transaction(write=True) as tx:
            self._lock_writes(tx)
            values = {'key_hash': key_hash, 'item_key': document_id}
            sql = 'DELETE FROM kb_storage_documents WHERE key_hash=:key_hash AND item_key=:item_key'
            if expected_revision is not None:
                sql += ' AND revision=:expected'
                values['expected'] = expected_revision
            deleted = tx.execute(sql, values).rowcount == 1
            if not deleted and expected_revision is not None:
                row = tx.execute('SELECT item_key FROM kb_storage_documents WHERE key_hash=:key_hash', values).first()
                if row is not None:
                    raise StorageError('STORAGE_REVISION_CONFLICT')
            return deleted


class SqlKeyValueStore(_SqlStore):
    def __init__(self, database):
        binary_type = {'sqlite': 'BLOB', 'postgresql': 'BYTEA', 'mysql': 'LONGBLOB'}.get(database.info.engine)
        if binary_type is None:
            raise StorageError('STORAGE_BACKEND_UNSUPPORTED')
        super().__init__(database, table='kb_storage_kv', fields='payload ' + binary_type + ' NOT NULL')

    def get(self, key):
        key_hash = _key_hash(key)
        with self._transaction() as tx:
            row = tx.execute('SELECT item_key,payload FROM kb_storage_kv WHERE key_hash=:key', {'key': key_hash}).first()
            if row is None:
                return None
            if (row['item_key'] != key or not isinstance(row['payload'], bytes) or
                    len(row['payload']) > MAX_VALUE_BYTES):
                raise StorageError('STORAGE_DATA_CORRUPT')
            return validate_value(row['payload'])

    def set(self, key, value):
        key_hash, payload = _key_hash(key), validate_value(value)
        updates = ('payload=VALUES(payload)',) if self._engine == 'mysql' else ('payload=excluded.payload',)
        with self._transaction(write=True) as tx:
            tx.execute(self._upsert(('key_hash', 'item_key', 'payload'), updates),
                       {'key_hash': key_hash, 'item_key': key, 'payload': payload})

    def delete(self, key):
        key_hash = _key_hash(key)
        with self._transaction(write=True) as tx:
            return tx.execute('DELETE FROM kb_storage_kv WHERE key_hash=:key_hash AND item_key=:item_key',
                              {'key_hash': key_hash, 'item_key': key}).rowcount == 1


def _snapshot_payload(source, documents):
    return {'format': 'storage-document-snapshot/v1',
            'source': {'space': [source.space.application_id, source.space.owner_id, source.space.space_id],
                       'dataset_id': source.dataset_id},
            'documents': [{'id': document.id, 'value': document.value, 'revision': document.revision}
                          for document in documents]}


def _snapshot_bytes(payload):
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise StorageError('STORAGE_INVALID_SNAPSHOT') from None
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise StorageError('STORAGE_LIMIT_EXCEEDED')
    return raw


@dataclass(frozen=True)
class DocumentSnapshot:
    payload: dict
    checksum: str

    def to_json(self):
        return _snapshot_bytes({'payload': self.payload, 'checksum': self.checksum})

    @classmethod
    def from_json(cls, raw):
        if not isinstance(raw, bytes) or len(raw) > MAX_SNAPSHOT_BYTES:
            raise StorageError('STORAGE_INVALID_SNAPSHOT')
        try:
            value = json.loads(raw)
            if set(value) != {'payload', 'checksum'}:
                raise ValueError('INVALID_ENVELOPE')
            snapshot = cls(value['payload'], value['checksum'])
            _validate_snapshot(snapshot)
            return snapshot
        except (ValueError, TypeError, KeyError, RecursionError):
            raise StorageError('STORAGE_INVALID_SNAPSHOT') from None


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, DocumentSnapshot) or type(snapshot.payload) is not dict:
        raise StorageError('STORAGE_INVALID_SNAPSHOT')
    raw = _snapshot_bytes(snapshot.payload)
    if sha256(raw).hexdigest() != snapshot.checksum:
        raise StorageError('STORAGE_INVALID_SNAPSHOT')
    payload = json.loads(raw)
    if (set(payload) != {'format', 'source', 'documents'} or payload['format'] != 'storage-document-snapshot/v1' or
            not isinstance(snapshot.checksum, str)):
        raise StorageError('STORAGE_INVALID_SNAPSHOT')
    source = payload['source']
    if (type(source) is not dict or set(source) != {'space', 'dataset_id'} or
            type(source['space']) is not list or len(source['space']) != 3 or
            any(type(item) is not str or not item or len(item) > 200 for item in source['space']) or
            type(source['dataset_id']) is not str or not source['dataset_id'] or len(source['dataset_id']) > 200):
        raise StorageError('STORAGE_INVALID_SNAPSHOT')
    documents = payload['documents']
    if type(documents) is not list or len(documents) > MAX_SNAPSHOT_ITEMS:
        raise StorageError('STORAGE_INVALID_SNAPSHOT')
    seen = set()
    for record in documents:
        if type(record) is not dict or set(record) != {'id', 'value', 'revision'}:
            raise StorageError('STORAGE_INVALID_SNAPSHOT')
        validate_key(record['id'])
        _document_json(record['value'])
        _revision(record['revision'], optional=False)
        if record['id'] in seen or record['revision'] < 1:
            raise StorageError('STORAGE_INVALID_SNAPSHOT')
        seen.add(record['id'])
    return payload


def export_documents(store, *, expected_dataset_id):
    if store.identity.dataset_id != expected_dataset_id:
        raise StorageError('STORAGE_DATASET_IDENTITY_MISMATCH')
    limits = QueryLimits(max_rows=MAX_SNAPSHOT_ITEMS + 1, max_bytes=MAX_SNAPSHOT_BYTES, max_statements=1)
    with store._transaction(limits=limits) as tx:
        rows = tx.execute('SELECT key_hash,item_key,payload,revision FROM kb_storage_documents ORDER BY key_hash '
                          'LIMIT ' + str(MAX_SNAPSHOT_ITEMS + 1)).rows
        if len(rows) > MAX_SNAPSHOT_ITEMS:
            raise StorageError('STORAGE_LIMIT_EXCEEDED')
        documents = tuple(_document(row) for row in rows)
    payload = _snapshot_payload(store.identity, documents)
    snapshot = DocumentSnapshot(payload, sha256(_snapshot_bytes(payload)).hexdigest())
    snapshot.to_json()
    return snapshot


def import_documents(store, snapshot, *, expected_source_dataset_id, expected_target_dataset_id):
    payload = _validate_snapshot(snapshot)
    documents = payload['documents']
    if (payload['source']['dataset_id'] != expected_source_dataset_id or
            store.identity.dataset_id != expected_target_dataset_id):
        raise StorageError('STORAGE_DATASET_IDENTITY_MISMATCH')
    limits = QueryLimits(max_rows=MAX_SNAPSHOT_ITEMS + 1, max_bytes=MAX_SNAPSHOT_BYTES,
                         max_statements=MAX_SNAPSHOT_ITEMS + 2)
    with store._transaction(write=True, limits=limits) as tx:
        store._lock_writes(tx)
        rows = tx.execute('SELECT key_hash,item_key,payload,revision FROM kb_storage_documents LIMIT ' +
                          str(MAX_SNAPSHOT_ITEMS + 1)).rows
        if rows:
            actual = {row['item_key']: _document(row) for row in rows}
            expected = {row['id']: Document(row['id'], row['value'], row['revision']) for row in documents}
            if actual != expected:
                raise StorageError('STORAGE_IMPORT_TARGET_NOT_EMPTY')
            return len(documents)
        for record in documents:
            tx.execute('INSERT INTO kb_storage_documents(key_hash,item_key,payload,revision) '
                       'VALUES(:key_hash,:item_key,:payload,:revision)',
                       {'key_hash': _key_hash(record['id']), 'item_key': record['id'],
                        'payload': _document_json(record['value']), 'revision': record['revision']})
    return len(documents)

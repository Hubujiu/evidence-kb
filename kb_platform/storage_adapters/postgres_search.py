from contextlib import contextmanager
from hashlib import sha256
import json
import math
import re
import struct
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from ..database_contracts import CommitUnknown, DatabaseError, QueryLimits
from ..search_errors import SearchUnavailable
from ..search_tokens import tokens
from ..storage_sdk import StorageError
from ..implementation_dependencies import implementation_dependencies
from .postgres_search_schema import postgres_search_schema


MAX_RECORDS = 10000
MAX_GENERATION_BYTES = 64 * 1024 * 1024
_LIMITS = QueryLimits(max_rows=MAX_RECORDS + 10, max_bytes=MAX_GENERATION_BYTES * 8,
                      max_statements=10, max_seconds=120)


def canonical_records(records):
    return json.dumps(sorted(records, key=lambda record: record['id']), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Invalid search schema identifier')
    return '"' + value.replace('"', '""') + '"'


def _float32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def _vector(value, dimension):
    if not isinstance(value, (list, tuple)) or len(value) != dimension or not any(value):
        raise ValueError('Invalid finite nonzero vector')
    try:
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in value):
            raise ValueError('Invalid finite nonzero vector')
        result = [_float32(number) for number in value]
    except (OverflowError, struct.error):
        raise ValueError('Invalid finite nonzero vector') from None
    if not any(result) or any(not math.isfinite(number) for number in result):
        raise ValueError('Invalid finite nonzero vector')
    return result


def _lexical_text(text, *, exact=False):
    return ' '.join('t' + _digest(token) for token in tokens(text, exact=exact))


def _projection(record):
    return {'body': _lexical_text(record['text']),
            'exact': _lexical_text(record['text'], exact=True),
            'heading': _lexical_text(record.get('heading', ''))}


def _validate_filters(filters):
    if (not isinstance(filters, dict) or set(filters) - {'eq', 'in', 'not_in'}
            or any(not isinstance(value, dict) for value in filters.values())
            or any(not isinstance(key, str) for value in filters.values() for key in value)
            or any(not isinstance(values, (list, tuple)) for operation in ('in', 'not_in')
                   for values in filters.get(operation, {}).values())):
        raise ValueError('Invalid bounded search query or filters')


def _matches(metadata, filters):
    return (all(metadata.get(key) == value for key, value in filters.get('eq', {}).items())
            and all(metadata.get(key) in values for key, values in filters.get('in', {}).items())
            and all(metadata.get(key) not in values for key, values in filters.get('not_in', {}).items()))


class PostgresSearchStore:
    def __init__(self, database, channel):
        if channel not in {'vector', 'lexical'}:
            raise ValueError('Unknown search channel')
        if database.info.engine != 'postgresql':
            raise StorageError('STORAGE_PROVIDER_MISMATCH')
        self.database, self.channel, self.identity = database, channel, database.identity
        self.schema = _identifier(database.physical_name)
        database.prepare(postgres_search_schema(self.identity.space.owner_id))
        database.seal()
        extension = 'vector' if channel == 'vector' else 'pg_textsearch'
        with self._transaction(write=False) as tx:
            installed = tx.execute('SELECT e.extversion,n.nspname FROM pg_extension e '
                'JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname=:extension',
                {'extension': extension}).fetchone()
        if installed is None or installed['nspname'] != 'public':
            raise StorageError('STORAGE_EXTENSION_REQUIRED', extension=extension, schema='public')
        self._extension = {'name': extension, 'version': installed['extversion']}

    @contextmanager
    def _transaction(self, *, write=False):
        try:
            with self.database.transaction(write=write, limits=_LIMITS) as tx:
                yield tx
        except CommitUnknown:
            raise
        except (SQLAlchemyError, DatabaseError) as error:
            raise SearchUnavailable('PostgreSQL search operation failed: ' + type(error).__name__) from error

    def storage_identity(self):
        return {'dataset': str(self.identity), 'channel': self.channel, 'format': 'postgres-generic-search/1'}

    def dependency_manifest(self):
        directory = Path(__file__).resolve().parents[1]
        return {'version': 1, 'implementation': {**implementation_dependencies(__file__,
            Path(__file__).with_name('postgres_search_schema.py'), directory / 'search_tokens.py',
            directory / 'storage_catalog.py', directory / 'database_contracts.py',
            directory / 'database_migrations.py', directory / 'database_providers/base.py',
            directory / 'database_providers/postgres.py', directory / 'storage_adapters/sql.py',
            packages=('psycopg', 'sqlalchemy', 'regex')), 'extension': dict(self._extension)},
            'storage': self.storage_identity()}

    def _generation(self, index):
        if not isinstance(index, str) or re.fullmatch(r'[\w][\w.-]{0,127}', index) is None:
            raise ValueError('Invalid bounded storage collection name')
        namespace = json.dumps(self.storage_identity(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return 'sg_' + _digest(namespace + '\0' + index)[:40]

    def _table(self, index):
        return self.schema + '.' + _identifier(self._generation(index))

    def _records(self, records, dimension):
        try:
            serialized = canonical_records(records)
            selected = json.loads(serialized)
            if (not selected or len(selected) > MAX_RECORDS
                    or len(serialized.encode('utf-8')) > MAX_GENERATION_BYTES
                    or len({record['id'] for record in selected}) != len(selected)):
                raise ValueError('Invalid records')
            if self.channel == 'vector' and dimension is not None and (
                    type(dimension) is not int or not 1 <= dimension <= 16000):
                raise ValueError('Invalid vector dimension')
            if self.channel == 'lexical' and dimension is not None:
                raise ValueError('Lexical records cannot contain a vector dimension')
            for record in selected:
                if (not isinstance(record['id'], str) or not record['id'] or len(record['id'].encode('utf-8')) > 512
                        or '\x00' in record['id'] or not isinstance(record.get('metadata'), dict)):
                    raise ValueError('Invalid record')
                if self.channel == 'vector':
                    if set(record) - {'id', 'metadata', 'vector'}:
                        raise ValueError('Vector records cannot contain document or lexical fields')
                    if 'vector' in record:
                        _vector(record['vector'], dimension)
                elif (set(record) - {'id', 'metadata', 'text', 'heading'}
                        or not isinstance(record.get('text'), str)
                        or not isinstance(record.get('heading', ''), str)):
                    raise ValueError('Invalid lexical record')
            return selected, serialized
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as error:
            raise SearchUnavailable('Invalid bounded search channel records') from error

    def stage(self, index, records, vector_dimension=None):
        generation = self._generation(index)
        records, serialized = self._records(records, vector_dimension)
        projected = [{'record': record, **(_projection(record) if self.channel == 'lexical' else {})}
                     for record in records]
        with self._transaction(write=True) as tx:
            tx.execute('SELECT ' + self.schema + '.storage_search_build('
                ':channel,:generation,CAST(:records AS jsonb),:dimension,:checksum)',
                {'channel': self.channel, 'generation': generation,
                 'records': json.dumps(projected, ensure_ascii=False, separators=(',', ':'), allow_nan=False),
                 'dimension': vector_dimension, 'checksum': _digest(serialized)})

    def _checked(self, index):
        generation = self._generation(index)
        projection = 'CAST(embedding AS text) AS vector_projection' if self.channel == 'vector' else 'body,exact,heading'
        with self._transaction() as tx:
            metadata = tx.execute('SELECT * FROM ' + self.schema + '._storage_search_generations '
                'WHERE generation=:generation AND channel=:channel AND ready',
                {'generation': generation, 'channel': self.channel}).fetchone()
            if metadata is None:
                raise SearchUnavailable('Search generation not found')
            rows = tx.execute('SELECT record_id,metadata,record,' + projection + ' FROM ' + self._table(index)).fetchall()
        try:
            records, serialized = self._records([row['record'] for row in rows], metadata['dimension'])
            if len(records) != metadata['record_count'] or _digest(serialized) != metadata['records_sha256']:
                raise ValueError('Search generation content mismatch')
            for row in rows:
                record = row['record']
                if row['record_id'] != record['id'] or row['metadata'] != record['metadata']:
                    raise ValueError('Search generation projection mismatch')
                if self.channel == 'lexical':
                    if any(row[field] != value for field, value in _projection(record).items()):
                        raise ValueError('Search generation lexical projection mismatch')
                else:
                    actual = None if row['vector_projection'] is None else json.loads(row['vector_projection'])
                    expected = None if 'vector' not in record else _vector(record['vector'], metadata['dimension'])
                    if (actual is None) != (expected is None) or actual is not None and _vector(actual, metadata['dimension']) != expected:
                        raise ValueError('Search generation vector projection mismatch')
            return metadata, records
        except (KeyError, TypeError, ValueError, SearchUnavailable) as error:
            raise SearchUnavailable('Search channel canonical content mismatch') from error

    def verify(self, index):
        metadata, records = self._checked(index)
        return {'count': len(records), 'dimension': metadata['dimension'],
                'units_sha256': metadata['records_sha256'], 'storage': self.storage_identity(),
                'generation': self._generation(index)}

    def read_records(self, index, ids=None):
        _, records = self._checked(index)
        if ids is None:
            return records
        by_id = {record['id']: record for record in records}
        return [by_id[identity] for identity in ids if identity in by_id]

    def seal(self, index, checksum):
        if not isinstance(checksum, str) or not 1 <= len(checksum) <= 1024 or '\x00' in checksum:
            raise ValueError('Invalid search seal')
        self.verify(index)
        with self._transaction(write=True) as tx:
            tx.execute('SELECT ' + self.schema + '.storage_search_seal(:generation,:checksum)',
                       {'generation': self._generation(index), 'checksum': checksum})

    def is_sealed(self, index, checksum):
        try:
            metadata, _ = self._checked(index)
            return isinstance(checksum, str) and metadata['seal'] == checksum
        except SearchUnavailable:
            return False

    def query(self, index, query, limit, *, filters=None):
        filters = {} if filters is None else filters
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid bounded search query or filters')
        _validate_filters(filters)
        metadata, records = self._checked(index)
        if self.channel == 'lexical' and not isinstance(query, str):
            raise ValueError('Invalid lexical query')
        if self.channel == 'vector':
            _vector(query, metadata['dimension'])
        allowed = {record['id']: record for record in records if _matches(record['metadata'], filters)}
        if not allowed:
            return []
        if self.channel == 'vector':
            statement = 'SELECT record_id,1-(embedding OPERATOR(public.<=>) CAST(:query AS public.vector))/2 AS score FROM '
            statement += self._table(index) + ' WHERE embedding IS NOT NULL AND record_id=ANY(:allowed) '
            statement += 'ORDER BY embedding OPERATOR(public.<=>) CAST(:query AS public.vector),record_id LIMIT :limit'
            params = {'query': json.dumps(query, allow_nan=False), 'allowed': list(allowed), 'limit': limit}
        else:
            fields = (('body', False, 1), ('exact', True, 1.5), ('heading', False, 2))
            expressions, params = [], {'allowed': list(allowed), 'limit': limit}
            for field, exact, weight in fields:
                text = _lexical_text(query, exact=exact)
                if not text:
                    continue
                params[field + '_query'] = text
                params[field + '_index'] = self.schema + '.' + _identifier(self._generation(index) + '_' + field)
                expressions.append('(SELECT record_id,-distance*' + str(weight) + ' AS score FROM '
                    '(SELECT record_id,' + field + ' OPERATOR(public.<@>) public.to_bm25query(:' + field + '_query,:' + field + '_index) '
                    'AS distance FROM ' + self._table(index) + ' WHERE record_id=ANY(:allowed) '
                    'ORDER BY distance,record_id LIMIT :limit) field_hits)')
            if not expressions:
                return []
            statement = 'SELECT record_id,max(score) AS score FROM (' + ' UNION ALL '.join(expressions)
            statement += ') fields WHERE score>0 GROUP BY record_id ORDER BY score DESC,record_id LIMIT :limit'
        with self._transaction() as tx:
            rows = tx.execute(statement, params).fetchall()
        result = []
        for row in rows:
            record = allowed.get(row['record_id'])
            if record is None or type(row['score']) not in (int, float) or not math.isfinite(row['score']):
                raise SearchUnavailable('Search channel returned unauthorized record or invalid score')
            result.append({'id': record['id'], 'metadata': record['metadata'], 'score': row['score']})
        return result

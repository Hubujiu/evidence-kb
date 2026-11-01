import hashlib
import json
import math
import re

from .search import OpenSearch, SearchUnavailable, binding_key, content_order


def canonical_records(records):
    return json.dumps(sorted(records, key=lambda record: record['id']), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _matches(metadata, filters):
    return (all(metadata.get(key) == value for key, value in filters.get('eq', {}).items())
            and all(metadata.get(key) in values for key, values in filters.get('in', {}).items())
            and all(metadata.get(key) not in values for key, values in filters.get('not_in', {}).items()))


class _SearchChannel:
    def __init__(self, backend, identity=None):
        self.backend = backend
        self.identity = identity if identity is not None else getattr(getattr(backend, 'database', None), 'identity', None)

    def storage_identity(self):
        if self.identity is not None:
            return {'dataset': str(self.identity), 'channel': self.channel}
        if callable(getattr(self.backend, 'storage_identity', None)):
            return {**self.backend.storage_identity(), 'channel': self.channel}
        return {'root_sha256': _digest(str(self.backend.root)), 'channel': self.channel}

    def _native_index(self, index):
        if not isinstance(index, str) or re.fullmatch(r'[\w][\w.-]{0,127}', index) is None:
            raise ValueError('Invalid bounded storage collection name')
        namespace = json.dumps(self.storage_identity(), ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'), allow_nan=False)
        return OpenSearch.index_name('storage', namespace, _digest(namespace + '\0' + index)[:32])

    def _unit(self, record):
        metadata = record['metadata']
        def text(key, default):
            value = metadata.get(key)
            return value if isinstance(value, str) and value else default

        unit = {'unit_id': record['id'], 'tenant_id': 'storage', 'kb_id': 'records',
                'binding_id': record['id'], 'source_id': record['id'],
                'source_version_id': text('source_version_id', record['id']),
                'parse_snapshot_id': text('parse_snapshot_id', record['id']),
                'ordinal': metadata['ordinal'] if type(metadata.get('ordinal')) is int else 0,
                'citation_id': text('citation_id', record['id']), 'kind': text('kind', 'Record'),
                'build_id': text('build_id', ''), 'record': record,
                'text': record.get('text', ''), 'heading_text': record.get('heading', '')}
        if 'vector' in record:
            unit['vector'] = record['vector']
        return unit

    def stage(self, index, records, vector_dimension=None):
        records = json.loads(canonical_records(records))
        if (not records or len({record['id'] for record in records}) != len(records)
                or any(not isinstance(record['id'], str) or not record['id']
                       or not isinstance(record.get('metadata'), dict) for record in records)):
            raise SearchUnavailable('Nonempty unique channel records required')
        if self.channel == 'lexical' and any(not isinstance(record.get('text'), str) or 'vector' in record for record in records):
            raise SearchUnavailable('Lexical records require text without vectors')
        if self.channel == 'vector' and any(set(record) - {'id', 'metadata', 'vector'} for record in records):
            raise SearchUnavailable('Vector records cannot contain document or lexical fields')
        if vector_dimension is not None and (type(vector_dimension) is not int or not 1 <= vector_dimension <= 16000):
            raise SearchUnavailable('Invalid vector dimension')
        for record in records:
            if 'vector' not in record:
                continue
            vector = record['vector']
            if (not isinstance(vector, list) or len(vector) != vector_dimension or not any(vector)
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
                raise SearchUnavailable('Invalid finite nonzero vector')
        self.backend.stage(self._native_index(index), [self._unit(record) for record in records],
                           vector_dimension=vector_dimension)

    def _checked_units(self, index):
        try:
            return self._read_checked_units(index)
        except (KeyError, TypeError, ValueError) as exc:
            raise SearchUnavailable('Search channel canonical content mismatch') from exc

    def _read_checked_units(self, index):
        native = self._native_index(index)
        manifest = self.backend.verify(native)
        units = self.backend.read_units(native)
        serialized = json.dumps(sorted(units, key=content_order), ensure_ascii=False,
                                sort_keys=True, separators=(',', ':'), allow_nan=False)
        if (manifest.get('units_sha256') != _digest(serialized)
                or manifest.get('count', manifest.get('unit_count')) != len(units)
                or len({unit['unit_id'] for unit in units}) != len(units)
                or any(unit != self._unit(unit['record']) for unit in units)):
            raise SearchUnavailable('Search channel canonical content mismatch')
        return manifest, units

    def read_records(self, index, ids=None):
        _, units = self._checked_units(index)
        records = [unit['record'] for unit in units]
        if ids is None:
            return sorted(records, key=lambda record: record['id'])
        by_id = {record['id']: record for record in records}
        return [by_id[id] for id in ids if id in by_id]

    def verify(self, index):
        manifest, units = self._checked_units(index)
        return {'count': len(units), 'dimension': manifest.get('dimension'),
                'units_sha256': _digest(canonical_records([unit['record'] for unit in units])),
                'storage': self.storage_identity(), 'generation': self._native_index(index)}

    def seal(self, index, checksum):
        self.verify(index)
        self.backend.seal(self._native_index(index), checksum)

    def is_sealed(self, index, checksum):
        try:
            self.verify(index)
            return self.backend.is_sealed(self._native_index(index), checksum)
        except SearchUnavailable:
            return False

    def query(self, index, query, limit, *, filters=None):
        filters = {} if filters is None else filters
        if (type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(filters, dict)
                or set(filters) - {'eq', 'in', 'not_in'}
                or any(not isinstance(value, dict) for value in filters.values())
                or any(not isinstance(values, (list, tuple)) for operation in ('in', 'not_in')
                       for values in filters.get(operation, {}).values())):
            raise ValueError('Invalid bounded search query or filters')
        _, units = self._checked_units(index)
        allowed = [unit for unit in units if _matches(unit['record']['metadata'], filters)]
        if not allowed:
            return []
        if self.channel == 'lexical':
            if not isinstance(query, str):
                raise ValueError('Invalid lexical query')
            hits = []
            for page in (False, True):
                selected = [unit for unit in allowed if (unit['kind'] == 'Page') == page]
                if selected:
                    hits.extend(self.backend.search(self._native_index(index), 'storage', 'records', selected, query, limit,
                                                    'Page' if page else None))
            hits.sort(key=lambda hit: (-hit['score'], content_order(hit)))
            hits = hits[:limit]
        else:
            hits = self.backend.dense(self._native_index(index), 'storage', 'records', allowed, query, limit)
        stored = {unit['unit_id']: unit for unit in allowed}
        results = []
        for hit in hits:
            unit = stored.get(hit.get('unit_id'))
            if unit is None or binding_key(hit) != binding_key(unit):
                raise SearchUnavailable('Search channel returned unauthorized record')
            canonical = {key: value for key, value in unit.items() if key != 'vector'}
            if canonical != {key: value for key, value in hit.items() if key != 'score'}:
                raise SearchUnavailable('Search channel hit content mismatch')
            results.append({'id': unit['unit_id'], 'metadata': unit['record']['metadata'], 'score': hit['score']})
        return results


class SearchVectorAdapter(_SearchChannel):
    channel = 'vector'


class SearchLexicalAdapter(_SearchChannel):
    channel = 'lexical'

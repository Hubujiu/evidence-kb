import hashlib
import json

from .search import (OpenSearch, SearchGenerationIncomplete, SearchUnavailable, backend_dependencies,
                     binding_key, content_order, lexical_results)
from .search_channels import canonical_records
from .search_evidence import canonical_units, unit_sha256


FORMAT = 'split-authoritative-search/2'
FILTER_FIELDS = ('tenant_id', 'kb_id', 'binding_id', 'source_id', 'source_version_id',
                 'parse_snapshot_id', 'kind', 'build_id', 'citation_id', 'ordinal')


def channel_records(units, channel):
    records = []
    for unit in units:
        metadata = {key: unit[key] for key in FILTER_FIELDS if key in unit}
        metadata.update(binding_key=binding_key(unit), source_sha256=unit_sha256(unit))
        record = {'id': unit['unit_id'], 'metadata': metadata}
        if channel == 'bm25':
            record.update(text=unit['text'], heading=unit.get('heading_text', ''))
        elif 'vector' in unit:
            record['vector'] = unit['vector']
        records.append(record)
    return records


class SplitSearch:
    index_name = staticmethod(OpenSearch.index_name)
    _index = staticmethod(OpenSearch._index)
    supports_resume_build = True

    def __init__(self, bm25, dense, source):
        self.bm25, self.vector, self.source = bm25, dense, source
        self._channels = (('bm25', bm25), ('dense', dense))
        self.supports_manifest_storage = getattr(source, 'supports_manifest_storage', False)

    @classmethod
    def channel_index(cls, index, channel):
        cls._index(index)
        if channel not in ('bm25', 'dense'):
            raise ValueError('Unknown search channel')
        suffix = hashlib.sha256((index + '\0' + channel).encode()).hexdigest()[:32]
        return index.rsplit('-', 1)[0] + '-' + suffix

    def stage(self, index, units, vector_dimension=None):
        expected = canonical_units(units)
        if not units or len({unit['unit_id'] for unit in units}) != len(units):
            raise SearchUnavailable('Nonempty unique units required')
        scope = (units[0]['tenant_id'], units[0]['kb_id'])
        if any((unit['tenant_id'], unit['kb_id']) != scope for unit in units):
            raise SearchUnavailable('Mixed generation scopes')
        try:
            metadata = self.source.verify(index)
        except SearchUnavailable as exc:
            if str(exc) != 'Search generation not found':
                raise
            self.source.stage(index, units, vector_dimension=vector_dimension)
        else:
            if canonical_units(self.source.read_units(index)) != expected or metadata.get('dimension') != vector_dimension:
                raise SearchUnavailable('Existing evidence source differs from frozen input')
        for channel, backend in self._channels:
            records = channel_records(units, channel)
            dimension = vector_dimension if channel == 'dense' else None
            target = self.channel_index(index, channel)
            try:
                metadata = backend.verify(target)
            except SearchUnavailable as exc:
                if str(exc) != 'Search generation not found':
                    raise
                backend.stage(target, records, vector_dimension=dimension)
            else:
                if (canonical_records(backend.read_records(target)) != canonical_records(records)
                        or metadata.get('dimension') != dimension):
                    raise SearchUnavailable('Existing search channel differs from frozen input: ' + channel)
        self.verify(index)

    def _read_generation(self, index):
        results, missing = {}, []
        sources = (('source', self.source, index),
                   *((channel, backend, self.channel_index(index, channel)) for channel, backend in self._channels))
        for name, backend, target in sources:
            try:
                metadata = backend.verify(target)
                records = backend.read_units(target) if name == 'source' else backend.read_records(target)
                results[name] = metadata, records
            except SearchUnavailable as exc:
                if str(exc) != 'Search generation not found':
                    raise
                missing.append(name)
        if len(missing) == len(sources):
            raise SearchUnavailable('Search generation not found')
        if missing:
            raise SearchGenerationIncomplete('Incomplete split search generation: ' + ', '.join(missing))
        source, units = results['source']
        if (len({unit['unit_id'] for unit in units}) != len(units)
                or source.get('units_sha256') != hashlib.sha256(canonical_units(units).encode()).hexdigest()
                or source.get('count', source.get('unit_count')) != len(units)):
            raise SearchUnavailable('Duplicate canonical search unit')
        for channel, _ in self._channels:
            metadata, records = results[channel]
            if canonical_records(records) != canonical_records(channel_records(units, channel)):
                raise SearchUnavailable('Search channel canonical content mismatch: ' + channel)
            dimension = source.get('dimension') if channel == 'dense' else None
            if metadata.get('dimension') != dimension:
                raise SearchUnavailable('Search channel vector dimensions differ')
        return units, results

    def read_units(self, index, ids=None):
        units, _ = self._read_generation(index)
        if ids is None:
            return sorted(units, key=content_order)
        by_id = {unit['unit_id']: unit for unit in units}
        return [by_id[id] for id in ids if id in by_id]

    def verify(self, index):
        units, results = self._read_generation(index)
        source = results['source'][0]
        return {'format': FORMAT, 'dimension': source.get('dimension'), 'count': len(units),
                'units_sha256': hashlib.sha256(canonical_units(units).encode()).hexdigest(),
                'source': {'index': index, 'storage': source.get('storage', backend_dependencies(self.source).get('storage')),
                           'units_sha256': hashlib.sha256(canonical_units(units).encode()).hexdigest()},
                'channels': {channel: {'index': self.channel_index(index, channel), **results[channel][0]}
                             for channel, _ in self._channels}}

    def _seal_token(self, index, checksum):
        serialized = json.dumps({'checksum': checksum, 'binding': self.verify(index)}, ensure_ascii=False,
                                sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(serialized.encode()).hexdigest()

    def seal(self, index, checksum):
        token = self._seal_token(index, checksum)
        for channel, backend in self._channels:
            target = self.channel_index(index, channel)
            if not backend.is_sealed(target, token):
                backend.seal(target, token)
        if not self.source.is_sealed(index, token):
            self.source.seal(index, token)
        if not self.is_sealed(index, checksum):
            raise SearchUnavailable('Split search sealing is incomplete')

    def is_sealed(self, index, checksum):
        try:
            token = self._seal_token(index, checksum)
            return (self.source.is_sealed(index, token)
                    and all(backend.is_sealed(self.channel_index(index, channel), token)
                            for channel, backend in self._channels))
        except SearchUnavailable:
            return False

    def publish_manifest(self, manifest, checksum):
        if not self.supports_manifest_storage:
            raise SearchUnavailable('No durable search manifest store is configured')
        index = manifest['index_name']
        if not self.is_sealed(index, checksum):
            raise SearchUnavailable('All search channels must be sealed before publication')
        binding = self.verify(index)
        envelope = {'index_name': index, 'tenant_id': manifest['tenant_id'], 'kb_id': manifest['kb_id'],
                    'storage_binding': binding, 'original_manifest': manifest, 'original_checksum': checksum}
        self.source.publish_manifest(envelope, self._seal_token(index, checksum))

    def current_manifest(self, tenant_id, kb_id):
        if not self.supports_manifest_storage:
            raise FileNotFoundError('No published semantic manifest')
        envelope, token = self.source.current_manifest(tenant_id, kb_id)
        manifest, checksum = envelope['original_manifest'], envelope['original_checksum']
        index = manifest['index_name']
        if (envelope['storage_binding'] != self.verify(index) or token != self._seal_token(index, checksum)
                or not self.is_sealed(index, checksum)):
            raise SearchUnavailable('Published search channels are incomplete or corrupt')
        return manifest, checksum

    @staticmethod
    def _filters(tenant, kb, allowed, kind=None, build=None, excluded=()):
        exact = {'tenant_id': tenant, 'kb_id': kb}
        if kind:
            exact['kind'] = kind
        if build:
            exact['build_id'] = build
        filters = {'eq': exact, 'in': {'binding_key': sorted({binding_key(row) for row in allowed})},
                   'not_in': {'citation_id': list(excluded)}}
        if kind is None:
            filters['not_in']['kind'] = ['Page']
        return filters

    def _hits(self, index, tenant, kb, allowed, hits):
        keys = {binding_key(row) for row in allowed}
        stored = {unit['unit_id']: unit for unit in self.read_units(index, [hit['id'] for hit in hits])}
        hydrated = []
        for hit in hits:
            unit = stored.get(hit['id'])
            if (unit is None or unit['tenant_id'] != tenant or unit['kb_id'] != kb or binding_key(unit) not in keys
                    or hit['metadata'] != channel_records([unit], 'dense')[0]['metadata']):
                raise SearchUnavailable('Search channel returned unauthorized or noncanonical evidence')
            hydrated.append({key: value for key, value in unit.items() if key != 'vector'} | {'score': hit['score']})
        return hydrated

    def search(self, index, tenant_id, kb_id, allowed, query, top_k, kind=None, *, exclude_citation_ids=()):
        if not allowed:
            return []
        hits = self.bm25.query(self.channel_index(index, 'bm25'), query, max(30, top_k),
                               filters=self._filters(tenant_id, kb_id, allowed, kind, excluded=exclude_citation_ids))
        return lexical_results(self._hits(index, tenant_id, kb_id, allowed, hits), query, top_k)

    def dense(self, index, tenant_id, kb_id, allowed, vector, top_k):
        if not allowed:
            return []
        filters = self._filters(tenant_id, kb_id, allowed)
        filters['not_in'].pop('kind')
        hits = self.vector.query(self.channel_index(index, 'dense'), vector, top_k, filters=filters)
        return self._hits(index, tenant_id, kb_id, allowed, hits)

    def dense_pages(self, index, tenant_id, kb_id, build_id, allowed, vector, top_k, excluded):
        if not allowed:
            return []
        hits = self.vector.query(self.channel_index(index, 'dense'), vector, top_k,
                                 filters=self._filters(tenant_id, kb_id, allowed, 'Page', build_id, excluded))
        return self._hits(index, tenant_id, kb_id, allowed, hits)

    def expand(self, index, tenant_id, kb_id, hit, limit=30):
        units = self.read_units(index)
        canonical = next((unit for unit in units if unit['unit_id'] == hit['unit_id']), None)
        if canonical is None or {key: value for key, value in canonical.items() if key != 'vector'} != {
                key: value for key, value in hit.items() if key != 'score'}:
            raise SearchUnavailable('Expansion hit differs from canonical evidence')
        parent = hit.get('parent_id') or hit['unit_id']
        candidates = [unit for unit in units if binding_key(unit) == binding_key(hit)
                      and (unit.get('parent_id') == parent or unit['unit_id'] in (parent, hit['unit_id']))]
        candidates = sorted(candidates, key=lambda unit: (unit['ordinal'], content_order(unit)))[:limit]
        hits = [{'id': unit['unit_id'], 'metadata': channel_records([unit], 'dense')[0]['metadata'],
                 'score': hit['score']} for unit in candidates]
        return self._hits(index, tenant_id, kb_id, [hit], hits)

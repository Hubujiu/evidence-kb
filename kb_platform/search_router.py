
import hashlib
import json

from .search import OpenSearch, SearchGenerationIncomplete, SearchUnavailable, binding_key, content_order


FORMAT = 'split-canonical-search/1'


def _canonical(units):
    return json.dumps(sorted(units, key=content_order), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


class SplitSearch:
    index_name = staticmethod(OpenSearch.index_name)
    _index = staticmethod(OpenSearch._index)
    supports_resume_build = True

    def __init__(self, bm25, dense):
        self.bm25, self.vector = bm25, dense
        self._channels = (('bm25', bm25), ('dense', dense))
        self._manifest_channel = next(((name, backend) for name, backend in self._channels
                                      if getattr(backend, 'supports_manifest_storage', False)), None)
        self.supports_manifest_storage = self._manifest_channel is not None

    @classmethod
    def channel_index(cls, index, channel):
        cls._index(index)
        if channel not in ('bm25', 'dense'):
            raise ValueError('Unknown search channel')
        suffix = hashlib.sha256((index + '\0' + channel).encode()).hexdigest()[:32]
        return index.rsplit('-', 1)[0] + '-' + suffix

    def stage(self, index, units, vector_dimension=None):
        expected = _canonical(units)
        if not units or len({unit['unit_id'] for unit in units}) != len(units):
            raise SearchUnavailable('Nonempty unique units required')
        scope = (units[0]['tenant_id'], units[0]['kb_id'])
        if any((unit['tenant_id'], unit['kb_id']) != scope for unit in units):
            raise SearchUnavailable('Mixed generation scopes')
        for channel, backend in self._channels:
            try:
                target = self.channel_index(index, channel)
                try:
                    metadata = backend.verify(target)
                    existing = backend.read_units(target)
                except SearchUnavailable as exc:
                    if str(exc) != 'Search generation not found':
                        raise
                    backend.stage(target, units, vector_dimension=vector_dimension)
                else:
                    if _canonical(existing) != expected or metadata.get('dimension') != vector_dimension:
                        raise SearchUnavailable('Existing search channel differs from frozen input: ' + channel)
            except SearchUnavailable:
                raise
            except (ValueError, OSError) as exc:
                raise SearchUnavailable('Search channel staging failed: ' + channel) from exc
        if _canonical(self.read_units(index)) != expected:
            raise SearchUnavailable('Staged search channels differ from canonical input')

    def _read_pair(self, index, ids=None):
        results, missing = [], []
        for channel, backend in self._channels:
            target = self.channel_index(index, channel)
            try:
                backend.verify(target)
                results.append(backend.read_units(target, ids))
            except SearchUnavailable as exc:
                if str(exc) != 'Search generation not found':
                    raise
                missing.append(channel)
        if len(missing) == 2:
            raise SearchUnavailable('Search generation not found')
        if missing:
            raise SearchGenerationIncomplete('Incomplete split search generation: ' + missing[0])
        if _canonical(results[0]) != _canonical(results[1]):
            raise SearchUnavailable('Search channel canonical content mismatch')
        if len({unit['unit_id'] for unit in results[0]}) != len(results[0]):
            raise SearchUnavailable('Duplicate canonical search unit')
        return results[0]

    def read_units(self, index, ids=None):
        ids = None if ids is None else list(ids)
        units = self._read_pair(index, ids)
        if ids is None:
            return sorted(units, key=content_order)
        by_id = {unit['unit_id']: unit for unit in units}
        return [by_id[unit_id] for unit_id in ids if unit_id in by_id]

    def verify(self, index):
        units = self.read_units(index)
        channels = {channel: backend.verify(self.channel_index(index, channel))
                    for channel, backend in self._channels}
        dimensions = {metadata.get('dimension') for metadata in channels.values()}
        if len(dimensions) != 1:
            raise SearchUnavailable('Search channel vector dimensions differ')
        return {'format': FORMAT, 'dimension': dimensions.pop(), 'count': len(units),
                'units_sha256': hashlib.sha256(_canonical(units).encode()).hexdigest(), 'channels': channels}

    def seal(self, index, checksum):
        self.verify(index)
        for channel, backend in self._channels:
            target = self.channel_index(index, channel)
            if not backend.is_sealed(target, checksum):
                backend.seal(target, checksum)
        if not self.is_sealed(index, checksum):
            raise SearchUnavailable('Split search sealing is incomplete')

    def is_sealed(self, index, checksum):
        try:
            self.verify(index)
            return all(backend.is_sealed(self.channel_index(index, channel), checksum)
                       for channel, backend in self._channels)
        except SearchUnavailable:
            return False

    def publish_manifest(self, manifest, checksum):
        if not self.supports_manifest_storage:
            raise SearchUnavailable('No durable search manifest store is configured')
        index = manifest['index_name']
        if not self.is_sealed(index, checksum):
            raise SearchUnavailable('All search channels must be sealed before publication')
        channel, backend = self._manifest_channel
        backend.publish_manifest(manifest, checksum, index=self.channel_index(index, channel))

    def current_manifest(self, tenant_id, kb_id):
        if not self.supports_manifest_storage:
            raise FileNotFoundError('No published semantic manifest')
        _, backend = self._manifest_channel
        manifest, checksum = backend.current_manifest(tenant_id, kb_id)
        if not self.is_sealed(manifest['index_name'], checksum):
            raise SearchUnavailable('Published search channels are incomplete or corrupt')
        return manifest, checksum

    def _hits(self, index, tenant, kb, allowed, hits):
        if not hits:
            return []
        keys = {binding_key(row) for row in allowed}
        if any(hit['tenant_id'] != tenant or hit['kb_id'] != kb or binding_key(hit) not in keys for hit in hits):
            raise SearchUnavailable('Search channel returned unauthorized evidence')
        stored = {unit['unit_id']: unit for unit in self.read_units(index, [hit['unit_id'] for hit in hits])}
        for hit in hits:
            canonical = {key: value for key, value in stored.get(hit['unit_id'], {}).items() if key != 'vector'}
            if canonical != {key: value for key, value in hit.items() if key != 'score'}:
                raise SearchUnavailable('Search hit differs from canonical evidence')
        return hits

    def search(self, index, tenant_id, kb_id, allowed, query, top_k, kind=None, *, exclude_citation_ids=()):
        if not allowed:
            return []
        hits = self.bm25.search(self.channel_index(index, 'bm25'), tenant_id, kb_id, allowed, query,
                                top_k, kind, exclude_citation_ids=exclude_citation_ids)
        return self._hits(index, tenant_id, kb_id, allowed, hits)

    def dense(self, index, tenant_id, kb_id, allowed, vector, top_k):
        if not allowed:
            return []
        hits = self.vector.dense(self.channel_index(index, 'dense'), tenant_id, kb_id, allowed, vector, top_k)
        return self._hits(index, tenant_id, kb_id, allowed, hits)

    def dense_pages(self, index, tenant_id, kb_id, build_id, allowed, vector, top_k, excluded):
        if not allowed:
            return []
        hits = self.vector.dense_pages(self.channel_index(index, 'dense'), tenant_id, kb_id, build_id,
                                       allowed, vector, top_k, excluded)
        return self._hits(index, tenant_id, kb_id, allowed, hits)

    def expand(self, index, tenant_id, kb_id, hit, limit=30):
        hits = self.bm25.expand(self.channel_index(index, 'bm25'), tenant_id, kb_id, hit, limit)
        return self._hits(index, tenant_id, kb_id, [hit], hits)

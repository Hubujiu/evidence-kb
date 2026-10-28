from __future__ import annotations

import hashlib
import json
import re

import httpx


TIE_BREAK_FIELDS = ('source_version_id', 'parse_snapshot_id', 'ordinal', 'unit_id')
BINDING_FIELDS = ('binding_id', 'source_id', 'source_version_id', 'parse_snapshot_id')
BULK_MAX_BYTES = 4 * 1024 * 1024
BULK_MAX_DOCUMENTS = 500
QUERY_PROTOCOL = 'bm25-then-query-blocks-distinct-idf/1'
PAGE_QUERY_PROTOCOL = 'bm25-isolated-page-fields-query-blocks/1'


def binding_key(item: dict) -> str:
    values = [item[field] for field in BINDING_FIELDS]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def content_order(hit: dict) -> tuple:
    return tuple(hit[field] for field in TIE_BREAK_FIELDS)


def score_order() -> list[dict]:
    return [{'_score': 'desc'}, *({field: 'asc'} for field in TIE_BREAK_FIELDS)]


class SearchUnavailable(RuntimeError):
    pass


def _bulk_batches(units):
    records, size = [], 0
    for unit in sorted(units, key=content_order):
        indexed = {**unit, 'binding_key': binding_key(unit)}
        if unit.get('kind') == 'Page':
            # Page navigation has its own BM25 field statistics. Adding a
            # whole-page object must not change the block field's IDF/avgdl.
            indexed['page_text'] = indexed.pop('text')
            indexed['original_page_text'] = indexed['page_text']
            indexed['page_heading'] = indexed.pop('heading_text', '')
        record = (json.dumps({'index': {'_id': unit['unit_id']}}) + '\n'
                  + json.dumps(indexed, ensure_ascii=False) + '\n').encode('utf-8')
        if len(record) > BULK_MAX_BYTES:
            raise SearchUnavailable('Index unit exceeds the bulk byte limit')
        if records and (size + len(record) > BULK_MAX_BYTES or len(records) >= BULK_MAX_DOCUMENTS):
            yield b''.join(records)
            records, size = [], 0
        records.append(record)
        size += len(record)
    if records:
        yield b''.join(records)


class OpenSearch:
    def __init__(self, url: str):
        self.url = url.rstrip('/')
        self._binding_key_indices = {}

    @staticmethod
    def index_name(tenant_id: str, kb_id: str, build_id: str) -> str:
        scope = hashlib.sha256(f'{tenant_id}\0{kb_id}'.encode()).hexdigest()[:24]
        return f'kb-{scope}-{build_id.replace("-", "")}'

    def _request(self, method: str, path: str, **kwargs):
        try:
            with httpx.Client(timeout=30, trust_env=False) as client:
                response = client.request(method, self.url + path, **kwargs)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailable('Search backend unavailable') from exc

    @staticmethod
    def _index(index: str) -> str:
        if not re.fullmatch(r'kb-[0-9a-f]{24}-[0-9a-f]{32}', index):
            raise ValueError('Invalid concrete index')
        return index

    def stage(self, index: str, units: list[dict], vector_dimension: int | None = None) -> None:
        index = self._index(index)
        keywords = ['tenant_id', 'kb_id', 'binding_id', 'source_id', 'source_version_id',
                    'parse_snapshot_id', 'unit_id', 'citation_id', 'kind', 'parent_id', 'build_id', 'binding_key']
        properties = {key: {'type': 'keyword'} for key in keywords}
        properties.update({
            'text': {'type': 'text', 'analyzer': 'cjk', 'fields': {'exact': {'type': 'text', 'analyzer': 'standard'}}},
            'heading_text': {'type': 'text', 'analyzer': 'cjk'},
            'page_text': {'type': 'text', 'analyzer': 'cjk', 'fields': {'exact': {'type': 'text', 'analyzer': 'standard'}}},
            'page_heading': {'type': 'text', 'analyzer': 'cjk'},
            'original_page_text': {'type': 'text', 'index': False},
            'ordinal': {'type': 'integer'},
        })
        settings = {'number_of_shards': 1, 'number_of_replicas': 0}
        if vector_dimension is not None:
            settings['index.knn'] = True
            properties['vector'] = {'type': 'knn_vector', 'dimension': vector_dimension,
                                    'method': {'name': 'hnsw', 'engine': 'lucene', 'space_type': 'cosinesimil',
                                               'parameters': {'ef_construction': 100, 'm': 16}}}
        self._request('PUT', '/' + index, json={
            'settings': settings,
            'mappings': {'dynamic': False, 'properties': properties},
        })
        if units:
            for batch in _bulk_batches(units):
                result = self._request('POST', f'/{index}/_bulk', content=batch,
                                       headers={'Content-Type': 'application/x-ndjson'})
                if result.get('errors') is not False:
                    raise SearchUnavailable('Index write failed')
            self._request('POST', f'/{index}/_refresh')
        count = self._request('GET', f'/{index}/_count')['count']
        if count != len(units):
            raise SearchUnavailable('Incomplete staged index')
        self._binding_key_indices[index] = True

    def _uses_binding_key(self, index: str) -> bool:
        index = self._index(index)
        if index not in self._binding_key_indices:
            mapping = self._request('GET', f'/{index}/_mapping')
            properties = mapping.get(index, {}).get('mappings', {}).get('properties', {})
            self._binding_key_indices[index] = properties.get('binding_key', {}).get('type') == 'keyword'
        return self._binding_key_indices[index]

    def search(self, index: str, tenant_id: str, kb_id: str, allowed: list[dict], query: str,
               top_k: int, kind: str | None = None, *, exclude_citation_ids=()) -> list[dict]:
        if (not isinstance(exclude_citation_ids, (list, tuple)) or len(exclude_citation_ids) > 50 or
                any(not isinstance(value, str) or not value or len(value) > 512 for value in exclude_citation_ids) or
                len(set(exclude_citation_ids)) != len(exclude_citation_ids)):
            raise ValueError('INVALID_EXCLUDED_CITATIONS')
        if not allowed:
            return []
        filters = self._filters(tenant_id, kb_id, allowed, compact=self._uses_binding_key(index))
        if kind:
            filters.append({'term': {'kind': kind}})
        # The Agent gateway may request five final units; both entrypoints use
        # the same bounded lexical pool before whole-unit query_blocks packing.
        fields = (['page_text', 'page_text.exact^2', 'page_heading^1.5'] if kind == 'Page'
                  else ['text', 'text.exact^2', 'heading_text^1.5'])
        body = {'size': max(30, top_k), '_source': {'excludes': ['vector']}, 'query': {'bool': {
            'filter': filters,
            'must': [{'multi_match': {'query': query, 'fields': fields}}],
        }}, 'sort': score_order()}
        if kind is None:
            body['query']['bool']['must_not'] = [{'term': {'kind': 'Page'}}]
        if exclude_citation_ids:
            body['query']['bool'].setdefault('must_not', []).append({'terms': {'citation_id': list(exclude_citation_ids)}})
        result = self._request('POST', f'/{self._index(index)}/_search', json=body)
        hits = [{**hit['_source'], 'score': hit['_score']} for hit in result['hits']['hits']]
        for hit in hits:
            if hit.get('kind') == 'Page':
                hit['text'] = hit.pop('original_page_text')
                hit.pop('page_text', None)
                hit['heading_text'] = hit.pop('page_heading', '')
        if all(isinstance(hit.get('text'), str) and isinstance(hit.get('evidence_ids'), list) for hit in hits):
            from .evidence_packing import rank_texts

            ordered = [hits[i] for i in rank_texts([hit['text'] for hit in hits], query)]
            # Preserve canonical parent context instead of spending the same
            # budget again on its typed-table child. The table-only channel
            # still returns all independently queryable typed table citations.
            groups = [hit for hit in ordered if hit.get('kind') != 'Table']
            hits = [hit for hit in ordered if hit.get('kind') != 'Table' or not any(
                all(hit.get(key) == parent.get(key) for key in BINDING_FIELDS) and
                set(hit['evidence_ids']) <= set(parent['evidence_ids']) for parent in groups)]
        return hits[:top_k]

    @staticmethod
    def _filters(tenant_id: str, kb_id: str, allowed: list[dict], *, compact=True) -> list[dict]:
        scope = [{'term': {'tenant_id': tenant_id}}, {'term': {'kb_id': kb_id}}]
        if compact:
            return scope + [{'terms': {'binding_key': sorted({binding_key(item) for item in allowed})}}]
        return scope + [
                   {'bool': {'minimum_should_match': 1, 'should': [
                       {'bool': {'filter': [{'term': {'binding_id': item['binding_id']}},
                                            {'term': {'source_id': item['source_id']}},
                                            {'term': {'source_version_id': item['source_version_id']}},
                                            {'term': {'parse_snapshot_id': item['parse_snapshot_id']}}]}}
                       for item in allowed]}}]

    def dense(self, index: str, tenant_id: str, kb_id: str, allowed: list[dict],
              vector: list[float], top_k: int) -> list[dict]:
        if not allowed:
            return []
        body = {'size': top_k, '_source': {'excludes': ['vector']}, 'sort': score_order(), 'query': {
            'knn': {'vector': {'vector': vector, 'k': top_k,
                                'filter': {'bool': {'filter': self._filters(
                                    tenant_id, kb_id, allowed, compact=self._uses_binding_key(index))}}}}}}
        result = self._request('POST', f'/{self._index(index)}/_search', json=body)
        return [{**hit['_source'], 'score': hit['_score']} for hit in result['hits']['hits']]

    def expand(self, index: str, tenant_id: str, kb_id: str, hit: dict, limit: int = 30) -> list[dict]:
        filters = [{'term': {'tenant_id': tenant_id}}, {'term': {'kb_id': kb_id}},
                   {'term': {'binding_id': hit['binding_id']}},
                   {'term': {'source_version_id': hit['source_version_id']}},
                   {'term': {'parse_snapshot_id': hit['parse_snapshot_id']}}]
        parent = hit.get('parent_id') or hit['unit_id']
        filters.append({'bool': {'minimum_should_match': 1, 'should': [
            {'term': {'parent_id': parent}}, {'term': {'unit_id': parent}},
            {'term': {'unit_id': hit['unit_id']}}]}})
        result = self._request('POST', f'/{self._index(index)}/_search', json={
            'size': limit, '_source': {'excludes': ['vector']},
            'query': {'bool': {'filter': filters}}, 'sort': [{'ordinal': 'asc'}],
        })
        return [{**item['_source'], 'score': hit['score']} for item in result['hits']['hits']]

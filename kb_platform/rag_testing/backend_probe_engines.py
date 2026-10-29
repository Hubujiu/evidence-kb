from __future__ import annotations

import json
import time

from .contracts import digest


class OpenSearchEngine:
    def __init__(self, rows, prefix, vectors=None, url='http://127.0.0.1:19200', ef=None):
        import httpx
        self.client = httpx.Client(base_url=url, trust_env=False, timeout=90)
        self.rows = rows
        self.names = {kb: prefix + '-' + digest(kb)[:12] for kb in {r['kb'] for r in rows}}
        self.vectors = vectors
        self.ef = ef

    def call(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def build(self):
        for kb, name in self.names.items():
            properties = {k: {'type': 'keyword'} for k in ('source_id', 'kb')}
            properties.update(text={'type': 'text', 'analyzer': 'cjk',
                                    'fields': {'exact': {'type': 'text', 'analyzer': 'standard'}}},
                              heading={'type': 'text', 'analyzer': 'cjk'}, rowid={'type': 'integer'})
            settings = {'number_of_shards': 1, 'number_of_replicas': 0}
            if self.vectors is not None:
                settings['index.knn'] = True
                properties['vector'] = {'type': 'knn_vector', 'dimension': self.vectors.shape[1],
                    'method': {'name': 'hnsw', 'engine': 'lucene', 'space_type': 'cosinesimil',
                               'parameters': {'m': 16, 'ef_construction': 100}}}
            self.call('PUT', '/' + name, json={'settings': settings, 'mappings': {'properties': properties}})
            selected = [r for r in self.rows if r['kb'] == kb]
            for offset in range(0, len(selected), 250):
                lines = []
                for row in selected[offset:offset + 250]:
                    value = dict(source_id=row['source_id'], kb=kb, text=row['text'],
                                 heading=row['heading'], rowid=row['id'])
                    if self.vectors is not None:
                        value['vector'] = self.vectors[row['id']].tolist()
                    lines.extend((json.dumps({'index': {'_id': str(row['id'])}}),
                                  json.dumps(value, ensure_ascii=False)))
                response = self.call('POST', f'/{name}/_bulk', content=('\n'.join(lines) + '\n').encode(),
                                     headers={'Content-Type': 'application/x-ndjson'})
                if response.get('errors'):
                    raise ValueError('OpenSearch bulk failure')
            self.call('POST', f'/{name}/_refresh')
            if self.call('GET', f'/{name}/_count')['count'] != len(selected):
                raise ValueError('OpenSearch count mismatch')

    def analyze(self, text, analyzer):
        return [t['token'] for t in self.call('POST', '/_analyze',
                                            json={'analyzer': analyzer, 'text': text})['tokens']]

    def search(self, request, k, vector=None):
        filt = {'bool': {'filter': [{'term': {'kb': request.kb}},
                                    {'terms': {'source_id': list(request.allowed_sources)}}]}}
        if vector is None:
            query = {'bool': {'filter': [filt], 'must': [{'multi_match': {
                'query': request.question, 'fields': ['text', 'text.exact^2', 'heading^1.5']}}]}}
        else:
            query = {'knn': {'vector': {'vector': vector, 'k': k, 'filter': filt}}}
            if self.ef is not None:
                query['knn']['vector']['method_parameters'] = {'ef_search': self.ef}
        result = self.call('POST', '/' + self.names[request.kb] + '/_search', json={
            'size': k, '_source': ['rowid'], 'query': query, 'sort': [{'_score': 'desc'}, {'rowid': 'asc'}]})
        return [h['_source']['rowid'] for h in result['hits']['hits']]


class TantivyEngine:
    def __init__(self, rows, directory, tokens, queries):
        import tantivy as tv
        self.tv, self.rows, self.directory = tv, rows, directory
        self.tokens, self.queries, self.indices = tokens, queries, {}

    def build(self):
        tv = self.tv
        schema = tv.SchemaBuilder()
        schema.add_integer_field('rowid', stored=True, fast=True)
        schema.add_text_field('source_id', tokenizer_name='raw')
        for field in ('body', 'exact', 'heading'):
            schema.add_text_field(field, tokenizer_name='whitespace')
        self.schema = schema.build()
        for kb in sorted({r['kb'] for r in self.rows}):
            path = self.directory / digest(kb)[:12]
            path.mkdir(parents=True)
            idx = tv.Index(self.schema, path=str(path))
            writer = idx.writer(heap_size=16_000_000, num_threads=1)
            for row in self.rows:
                if row['kb'] != kb:
                    continue
                data = self.tokens[str(row['id'])]
                writer.add_document(tv.Document(rowid=row['id'], source_id=row['source_id'],
                    body=' '.join(data['body']), exact=' '.join(data['exact']), heading=' '.join(data['heading'])))
            writer.commit()
            writer.wait_merging_threads()
            idx.reload()
            self.indices[kb] = idx

    def search(self, request, k, vector=None):
        tv = self.tv
        fields = []
        for field, boost in (('body', 1), ('exact', 2), ('heading', 1.5)):
            tokens = self.queries[request.case_id]['exact' if field == 'exact' else 'body']
            terms = [(tv.Occur.Should, tv.Query.term_query(self.schema, field, t)) for t in tokens]
            if terms:
                fields.append(tv.Query.boost_query(tv.Query.boolean_query(terms), boost))
        if not fields or not request.allowed_sources:
            return []
        query = tv.Query.boolean_query([
            (tv.Occur.Must, tv.Query.disjunction_max_query(fields, 0.0)),
            (tv.Occur.Must, tv.Query.const_score_query(
                tv.Query.term_set_query(self.schema, 'source_id', request.allowed_sources), 0.0))])
        searcher = self.indices[request.kb].searcher()
        result = searcher.search(query, limit=k)
        return [searcher.doc(address)['rowid'][0] for _, address in result.hits]


class DenseEngine:
    def __init__(self, rows, vectors, kind, prefix, directory, url='http://127.0.0.1:16333'):
        self.rows, self.vectors, self.kind = rows, vectors, kind
        self.prefix, self.directory, self.url = prefix, directory, url

    def build(self):
        import numpy as np
        self.indices = {}
        if self.kind == 'exact':
            return
        if self.kind.startswith('usearch'):
            from usearch.index import Index

            for kb in sorted({r['kb'] for r in self.rows}):
                ids = np.asarray([r['id'] for r in self.rows if r['kb'] == kb], dtype=np.uint64)
                index = Index(ndim=self.vectors.shape[1], metric='cos', dtype='f32',
                              connectivity=16, expansion_add=100, expansion_search=128)
                index.add(ids, self.vectors[ids], threads=1)
                index.save(str(self.directory / (digest(kb)[:12] + '.usearch')))
                self.indices[kb] = index
            return
        from qdrant_client import QdrantClient, models as qm
        self.qm = qm
        self.client = QdrantClient(url=self.url, timeout=90, trust_env=False)
        self.client.create_collection(self.prefix,
            vectors_config=qm.VectorParams(size=self.vectors.shape[1], distance=qm.Distance.COSINE),
            hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=100, full_scan_threshold=10000),
            optimizers_config=qm.OptimizersConfigDiff(indexing_threshold=1))
        for field in ('kb', 'source_id'):
            self.client.create_payload_index(self.prefix, field, qm.PayloadSchemaType.KEYWORD, wait=True)
        for offset in range(0, len(self.rows), 250):
            self.client.upsert(self.prefix, [qm.PointStruct(id=r['id'], vector=self.vectors[r['id']].tolist(),
                payload={'kb': r['kb'], 'source_id': r['source_id']}) for r in self.rows[offset:offset + 250]], wait=True)
        deadline = time.monotonic() + 180
        while True:
            info = self.client.get_collection(self.prefix)
            if info.status == qm.CollectionStatus.GREEN and (info.indexed_vectors_count or 0) >= len(self.rows):
                break
            if time.monotonic() > deadline:
                raise ValueError('Qdrant HNSW not fully built: ' + str(info))
            time.sleep(1)
        self.build_info = info.model_dump(mode='json')

    def search(self, request, k, vector=None):
        import numpy as np
        allowed = set(request.allowed_sources)
        ids = np.asarray([r['id'] for r in self.rows if r['kb'] == request.kb and r['source_id'] in allowed])
        if len(ids) == 0:
            return []
        query = np.asarray(vector, dtype=np.float32)
        if self.kind == 'exact':
            scores = self.vectors[ids] @ query
            return ids[np.argsort(-scores, kind='stable')[:k]].tolist()
        if self.kind.startswith('usearch'):
            complete_scope = {r['source_id'] for r in self.rows if r['kb'] == request.kb}
            if allowed != complete_scope:

                scores = self.vectors[ids] @ query
                return ids[np.argsort(-scores, kind='stable')[:k]].tolist()
            return self.indices[request.kb].search(query, k, threads=1).keys.tolist()
        qm = self.qm
        filt = qm.Filter(must=[qm.FieldCondition(key='kb', match=qm.MatchValue(value=request.kb)),
            qm.FieldCondition(key='source_id', match=qm.MatchAny(any=list(request.allowed_sources)))])
        result = self.client.query_points(self.prefix, query=vector, query_filter=filt, limit=k,
            search_params=qm.SearchParams(hnsw_ef=128, exact=False), with_payload=False, with_vectors=False)
        return [p.id for p in result.points]


class EmbeddedEngine:

    def __init__(self, rows, directory, vectors=None, *, backend=None, generation_key=''):
        from kb_platform.embedded_search import EmbeddedSearch
        self.backend = backend or EmbeddedSearch(directory)
        self.generation_key = generation_key
        self.rows, self.vectors = rows, vectors
        self.names, self.bindings = {}, {}

    def build(self):
        for kb in sorted({r['kb'] for r in self.rows}):
            name = self.backend.index_name('probe', kb, digest(self.generation_key + kb)[:32])
            self.names[kb] = name
            units = []
            for r in self.rows:
                if r['kb'] != kb:
                    continue
                binding = dict(binding_id=r['source_id'], source_id=r['source_id'],
                    source_version_id=r['version'], parse_snapshot_id=r['source_sha256'])
                self.bindings[r['source_id']] = binding
                unit = dict(**binding, tenant_id='probe', kb_id=kb, build_id='probe', unit_id=str(r['id']),
                    citation_id=str(r['id']), ordinal=r['id'], kind='Text', text=r['text'], heading_text=r['heading'])
                if self.vectors is not None:
                    unit['vector'] = self.vectors[r['id']].tolist()
                units.append(unit)
            self.backend.stage(name, units, self.vectors.shape[1] if self.vectors is not None else None)

    def search(self, request, k, vector=None):
        allowed = [self.bindings[s] for s in request.allowed_sources]
        method = self.backend.dense if vector is not None else self.backend.search
        hits = method(self.names[request.kb], 'probe', request.kb, allowed,
                      vector if vector is not None else request.question, k)
        return [int(h['unit_id']) for h in hits]

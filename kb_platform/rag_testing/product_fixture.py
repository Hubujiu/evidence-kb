"""In-memory authority fixture for current product-chain contracts.

Uses actual parsing, canonical units, gateway, retrieval and service code.
Storage/search transport is deterministic; this is not live ACL acceptance.
"""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from kb_platform.agent_tools import EvidenceGateway
from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.documents import parse_native_document

class Encoder:
    provider_id = 'google_gemini'
    dimension = 3
    manifest = {'provider': provider_id, 'model': 'explicit-test-contract', 'dimension': 3}

    def __init__(self):
        self.queries = []

    def query(self, query):
        self.queries.append(query)
        return [1.0, 0.0, 0.0]


class Reranker:
    provider_id = 'magpie_luna'
    manifest = {'provider': provider_id, 'model': 'explicit-test-contract'}

    def __init__(self):
        self.inputs = []

    def rank(self, query, candidates):
        self.inputs.append(deepcopy(candidates))
        return [{'citation_id': item['citation_id'], 'score': float(index)}
                for index, item in enumerate(candidates)]


class Authority:
    _allowed = Platform._allowed

    def __init__(self, sources):
        self.actor, self.scope, self.revision = Actor('tenant', 'reader'), 'scope', 1
        self.embedding_provider = Encoder()
        self.rows, self.units, self.records = [], [], {}
        for number, (filename, text) in enumerate(sources):
            document = parse_native_document(text if isinstance(text, bytes) else text.encode(), filename)
            row = {'binding_id': f'b{number}', 'source_id': f's{number}',
                'source_version_id': document['source_version_id'],
                'parse_snapshot_id': document['parse_snapshot_id'], 'document': document,
                'owner_id': 'reader', 'readers': [], 'filename': filename,
                'policy': {'llm_context': True, 'rerank': True, 'embedding': True,
                           'allowed_providers': ['magpie_luna', 'google_gemini']}}
            self.rows.append(row)
            units, citations = Platform._units(self, self.actor, self.scope, 'build', [row])
            self.units.extend(units)
            for citation in citations:
                keys = ('tenant_id', 'kb_id', 'citation_id', 'build_id', 'binding_id', 'source_id',
                        'source_version_id', 'parse_snapshot_id', 'element_id', 'text',
                        'evidence_ids', 'anchors', 'table_data')
                record = dict(zip(keys, citation, strict=True))
                for key in ('evidence_ids', 'anchors', 'table_data'):
                    record[key] = record[key].obj
                self.records[record['citation_id']] = {**record, 'policy': row['policy'],
                    'original_sha256': row['source_version_id']}
        self.calls = []
        self.backend = SimpleNamespace(search=self.search, dense=self.dense)
        self.build = {'index_name': 'explicit-test-index', 'inputs': {
            'channels': ['bm25', 'dense'], 'embedding': self.embedding_provider.manifest,
            'vector_source_ids': [row['source_id'] for row in self.rows]}}

    _build_config = staticmethod(Platform._build_config)

    @contextmanager
    def _tx(self, actor, scope):
        yield None

    def _lock_authority(self, conn, actor):
        return self.revision

    def _auth(self, conn, actor, scope):
        return {'manifest_id': 'build'}

    def _bindings(self, conn, actor, scope):
        return self.rows

    def resolve_citation(self, actor, scope, citation_id, purpose):
        record = deepcopy(self.records[citation_id])
        if record['source_id'] not in {row['source_id'] for row in self._allowed(self.rows, actor, purpose)}:
            raise PlatformError('ACCESS_DENIED', 'revoked', 403)
        return record

    def _hits(self, allowed, limit, kind=None, exclude=()):
        sources = {row['source_id'] for row in allowed}
        return [{**deepcopy(unit), 'score': 1.0} for unit in self.units
                if unit['source_id'] in sources and
                (unit['kind'] == kind if kind else unit['kind'] != 'Page') and
                unit['citation_id'] not in exclude][:limit]

    def search(self, index, tenant, scope, allowed, query, limit, kind=None, **kwargs):
        self.calls.append(('bm25', [row['source_id'] for row in allowed]))
        return self._hits(allowed, limit, kind, kwargs.get('exclude_citation_ids', ()))

    def dense(self, index, tenant, scope, allowed, vector, limit):
        self.calls.append(('dense', [row['source_id'] for row in allowed]))
        return list(reversed(self._hits(allowed, limit)))


class Gateway(EvidenceGateway):
    def _state(self, actor, scope_id, *, with_build=False, with_bindings=True):
        return ({'scope_id': scope_id, 'manifest_id': 'build', 'authority_revision': self.platform.revision},
                self.platform.rows, self.platform.build if with_build else None)



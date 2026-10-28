"""Authorized original evidence and bounded, read-only tools for agent sessions.

The lexical gateway intentionally does not invoke the research reranker or use its
cached source policies. Source restrictions are applied to the backend query,
before ranking, and every returned passage is checked against the authority.
"""
from __future__ import annotations

import ast
from decimal import Decimal, DecimalException, Subnormal, Underflow, localcontext
from hashlib import sha256
import json
import math
from uuid import NAMESPACE_URL, uuid5

from .core import Platform, PlatformError
from .search import BINDING_FIELDS, SearchUnavailable


EVIDENCE_FIELDS = ('citation_id', 'source_id', 'source_version_id', 'text', 'anchors',
                   'build_id', 'parse_snapshot_id', 'evidence_ids', 'original_sha256', 'doc_id', 'evidence_id')
SPAN_FIELDS = ('parse_text_start', 'parse_text_end', 'source_text_sha256', 'kind')
RETRIEVAL_METHOD = 'authorized_bm25'


def evidence_record(record):
    """Project public original evidence, retaining only proved optional ranges."""
    return {key: record[key] for key in EVIDENCE_FIELDS} | {
        key: record[key] for key in SPAN_FIELDS if key in record}


def canonical_span_metadata(record, document, element_id):
    """Derive exact LF parse coordinates from element identity, never text search.

    Repeated paragraphs must keep their own offsets. Older/unknown parse or
    citation representations remain without range fields and cannot be merged
    by a containment heuristic.
    """
    from .delivery_units import indexed_elements

    if (not isinstance(document, dict) or not isinstance(document.get('elements'), list)
            or document.get('source_version_id') != record['source_version_id']
            or document.get('parse_snapshot_id') != record['parse_snapshot_id']):
        return {}
    elements = document['elements']
    if (not elements or any(not isinstance(element, dict)
                           or not isinstance(element.get('raw_text'), str)
                           or type(element.get('ordinal')) is not int
                           or not isinstance(element.get('element_id'), str)
                           or not isinstance(element.get('kind'), str) for element in elements)
            or [element['ordinal'] for element in elements] != sorted({element['ordinal'] for element in elements})
            or len({element.get('element_id') for element in elements}) != len(elements)):
        return {}
    positions, cursor = {}, 0
    for element in elements:
        positions[element['element_id']] = (cursor, cursor + len(element['raw_text']))
        cursor += len(element['raw_text']) + 1
    text = '\n'.join(element['raw_text'] for element in elements)
    try:
        matches = [element for element in indexed_elements(document, include_pages=True)
                   if element['element_id'] == element_id]
    except (KeyError, TypeError, ValueError):
        return {}
    if len(matches) != 1:
        return {}
    element = matches[0]
    if 'parse_text_start' in element or 'parse_text_end' in element:
        start, end = element.get('parse_text_start'), element.get('parse_text_end')
    else:
        start, end = positions.get(element_id, (None, None))
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or text[start:end] != record['text'] or element.get('raw_text') != record['text']):
        return {}
    return {'parse_text_start': start, 'parse_text_end': end,
            'source_text_sha256': sha256(text.encode('utf-8')).hexdigest(), 'kind': element['kind']}


def _policy(value):
    if not isinstance(value, dict) or set(value) - {
            'mode', 'source_ids', 'versions', 'version_policy', 'lifetime'}:
        raise PlatformError('INVALID_SOURCE_POLICY', 'Invalid source policy', 422)
    policy = {'mode': 'AUTO', 'source_ids': [], 'versions': {},
              'version_policy': 'latest', 'lifetime': 'session', **value}
    sources, versions = policy['source_ids'], policy['versions']
    if (policy['mode'] not in ('AUTO', 'PREFER', 'LOCKED') or
            policy['version_policy'] not in ('latest', 'pinned') or
            policy['lifetime'] not in ('turn', 'topic', 'session') or
            not isinstance(sources, list) or len(sources) > 100 or
            any(not isinstance(item, str) or not item for item in sources) or
            len(set(sources)) != len(sources) or not isinstance(versions, dict) or len(versions) > 100 or
            any(not isinstance(key, str) or not key or not isinstance(version, str) or not version
                for key, version in versions.items()) or
            policy['mode'] == 'LOCKED' and not sources or
            policy['version_policy'] == 'pinned' and
            (not versions or any(source not in versions for source in sources))):
        raise PlatformError('INVALID_SOURCE_POLICY', 'Invalid or unpinned source policy', 422)
    return policy


class EvidenceGateway:
    """All model-visible document reads cross this authorization boundary."""

    def __init__(self, platform, research=None, provider_id='magpie_luna', *,
                 embedding_provider=None, pipeline_reranker=None):
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError('A model provider is required for evidence authorization')
        self.platform, self.research, self.provider_id = platform, research, provider_id
        self.embedding_provider = embedding_provider
        self.pipeline_reranker = pipeline_reranker

    def _state(self, actor, scope_id, *, with_build=False, with_bindings=True):
        with self.platform._tx(actor, scope_id) as conn:
            revision = self.platform._lock_authority(conn, actor)
            kb = self.platform._auth(conn, actor, scope_id)
            if with_build and not kb['manifest_id']:
                raise PlatformError('NO_PUBLISHED_MANIFEST', 'Build this scope before using evidence', 409)
            state = {'scope_id': scope_id, 'manifest_id': kb['manifest_id'], 'authority_revision': revision}
            rows = []
            if with_bindings:
                rows = conn.execute(
                    "SELECT b.*,s.owner_id,s.readers,s.policy,v.filename,"
                    "jsonb_build_object('processing_policy',p.document->'processing_policy') AS document "
                    'FROM bindings b JOIN sources s USING (tenant_id,source_id) '
                    'JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) '
                    'JOIN source_versions v USING (tenant_id,source_id,source_version_id) '
                    "WHERE b.tenant_id=%s AND b.kb_id=%s AND b.status='ACTIVE' ORDER BY b.binding_id",
                    (actor.tenant_id, scope_id)).fetchall()
            build = None
            if with_build:
                build = conn.execute('SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s '
                                     "AND status='PUBLISHED'", (actor.tenant_id, scope_id,
                                                               state['manifest_id'])).fetchone()
                if not build:
                    raise PlatformError('INVALID_MANIFEST', 'Published index unavailable', 503)
                if 'bm25' not in self.platform._build_config(build)['channels']:
                    raise PlatformError('CHANNEL_NOT_ADMITTED', 'Lexical retrieval was not published', 409)
        return state, rows, build

    def authorize(self, actor, scope_id):
        state, _, _ = self._state(actor, scope_id, with_bindings=False)
        return state

    def sources(self, actor, scope_id):
        state, rows, _ = self._state(actor, scope_id)
        mapping = self._mapping(actor, scope_id)
        result = []
        for row in self._allowed(actor, rows, _policy({})):
            metadata = self._registered(mapping, row['source_id'])
            result.append({'source_id': row['source_id'], 'source_version_id': row['source_version_id'],
                           'doc_id': metadata['doc_id'], 'title': metadata.get('title') or row['filename']})
        self._unchanged(actor, scope_id, state)
        return result

    def _unchanged(self, actor, scope_id, state):
        if self.authorize(actor, scope_id) != state:
            raise PlatformError('SCOPE_CHANGED', 'Authorization or published version changed; retry', 409)

    def _allowed(self, actor, rows, policy):
        rows = self.platform._allowed(rows, actor, 'llm_context')
        return [row for row in rows
                if self.provider_id in row['policy'].get('allowed_providers', [])
                and (policy['mode'] != 'LOCKED' or row['source_id'] in policy['source_ids'])]

    def _read_one(self, actor, scope_id, citation_id, policy, state, bindings):
        record = self.platform.resolve_citation(actor, scope_id, citation_id, purpose='llm_context')
        source = record['source_id']
        if (self.provider_id not in record['policy'].get('allowed_providers', []) or
                policy['mode'] == 'LOCKED' and source not in policy['source_ids']):
            raise PlatformError('ACCESS_DENIED', 'Evidence is not authorized for this model context', 403)
        if policy['version_policy'] == 'pinned':
            if policy['versions'].get(source) != record['source_version_id']:
                raise PlatformError('SOURCE_VERSION_MISMATCH', 'Evidence does not match the pinned version', 409)
        elif (record['build_id'] != state['manifest_id'] or
              not any(row['source_id'] == source and row['source_version_id'] == record['source_version_id']
                      and row['parse_snapshot_id'] == record['parse_snapshot_id'] for row in bindings)):
            raise PlatformError('SOURCE_VERSION_MISMATCH', 'Evidence is not from the current published version', 409)
        if not record['text'].strip() or not record['evidence_ids'] or not record['anchors']:
            raise PlatformError('SOURCE_INTEGRITY', 'Original evidence is incomplete', 503)
        record['doc_id'] = self._doc_id(actor, scope_id, source)
        record['evidence_id'] = record['citation_id']
        for key in SPAN_FIELDS:
            record.pop(key, None)
        record.update(self._canonical_metadata(actor, scope_id, record, bindings))
        return record

    def _canonical_metadata(self, actor, scope_id, record, bindings):
        """Read only the already authorized immutable parse for this citation."""
        from .delivery_units import indexed_elements

        row = next((item for item in bindings if all(item.get(key) == record[key] for key in
                    ('source_id', 'source_version_id', 'parse_snapshot_id'))), None)
        document = row.get('document') if row is not None else None
        element_id = record.get('element_id')
        if isinstance(document, dict) and isinstance(document.get('elements'), list):
            if element_id is None and row.get('binding_id'):
                # Same identity rule as Platform._units. No ambiguous str.find
                # lookup is permitted, even for a unique-looking passage.
                try:
                    candidates = [element['element_id'] for element in indexed_elements(document, include_pages=True)
                        if str(uuid5(NAMESPACE_URL,
                            f'{record["build_id"]}/{row["binding_id"]}/{element["element_id"]}')) == record['citation_id']]
                except (KeyError, TypeError, ValueError):
                    candidates = []
                element_id = candidates[0] if len(candidates) == 1 else None
            return canonical_span_metadata(record, document, element_id)
        # Custom authorities may only expose a citation contract. Unknown ranges
        # remain independent; the standard Platform can retrieve its immutable
        # parse by exact citation/source/version/parse identity, including pinned
        # old citations that no longer equal the active binding's parse.
        if not isinstance(self.platform, Platform):
            return {}
        with self.platform._tx(actor, scope_id) as conn:
            self.platform._auth(conn, actor, scope_id)
            proof = conn.execute('SELECT c.element_id,p.document FROM citations c JOIN parse_snapshots p '
                'USING (tenant_id,source_id,source_version_id,parse_snapshot_id) '
                'WHERE c.tenant_id=%s AND c.kb_id=%s AND c.citation_id=%s AND c.build_id=%s '
                'AND c.source_id=%s AND c.source_version_id=%s AND c.parse_snapshot_id=%s',
                (actor.tenant_id, scope_id, record['citation_id'], record['build_id'], record['source_id'],
                 record['source_version_id'], record['parse_snapshot_id'])).fetchone()
        return canonical_span_metadata(record, proof['document'], proof['element_id']) if proof is not None else {}

    def _doc_id(self, actor, scope_id, source_id):
        return self._registered(self._mapping(actor, scope_id), source_id)['doc_id']

    def _mapping(self, actor, scope_id):
        if self.research is None:
            return {}
        path = self.research._directory(actor, scope_id) / 'sources.json'
        if not path.is_file():
            return {}
        try:
            mapping = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(mapping, dict):
                raise ValueError()
            return mapping
        except (OSError, ValueError) as exc:
            raise PlatformError('INVALID_DOCUMENT_MAPPING', 'Registered source metadata is invalid', 503) from exc

    @staticmethod
    def _registered(mapping, source_id):
        try:
            item = mapping.get(source_id)
            if item is None:
                return {'doc_id': source_id}
            doc_id = item['doc_id']
            if (not isinstance(doc_id, str) or not doc_id or len(doc_id) > 200 or
                    'title' in item and (not isinstance(item['title'], str) or len(item['title']) > 500) or
                    sum(isinstance(value, dict) and value.get('doc_id') == doc_id
                        for value in mapping.values()) != 1):
                raise ValueError()
            return item
        except (ValueError, KeyError, TypeError) as exc:
            raise PlatformError('INVALID_DOCUMENT_MAPPING', 'Registered source metadata is invalid', 503) from exc

    @staticmethod
    def _ids(citation_ids):
        if (not isinstance(citation_ids, list) or len(citation_ids) > 50 or
                any(not isinstance(value, str) or not value for value in citation_ids) or
                len(set(citation_ids)) != len(citation_ids)):
            raise PlatformError('INVALID_CITATIONS', 'Provide at most 50 unique citation identifiers', 422)

    def read(self, actor, scope_id, citation_ids, policy):
        policy = _policy(policy)
        self._ids(citation_ids)
        state, bindings, _ = self._state(actor, scope_id)
        records = [self._read_one(actor, scope_id, citation, policy, state, bindings)
                   for citation in citation_ids]
        self._unchanged(actor, scope_id, state)
        return [evidence_record(record) for record in records]

    def revalidate(self, actor, scope_id, evidence, policy):
        if (not isinstance(evidence, list) or len(evidence) > 50 or
                any(not isinstance(item, dict) or any(key not in item for key in EVIDENCE_FIELDS)
                    for item in evidence)):
            raise PlatformError('INVALID_EVIDENCE', 'Complete original evidence references are required', 422)
        records = self.read(actor, scope_id, [item['citation_id'] for item in evidence], policy)
        if any(any(item[key] != record[key] for key in EVIDENCE_FIELDS)
               for item, record in zip(evidence, records, strict=True)):
            raise PlatformError('EVIDENCE_MISMATCH', 'Stored evidence differs from the original citation', 503)
        if any(any(key in item and (key not in record or item[key] != record[key]) for key in SPAN_FIELDS)
               for item, record in zip(evidence, records, strict=True)):
            raise PlatformError('EVIDENCE_MISMATCH', 'Stored canonical range differs from the current original parse', 503)
        return records

    def search_pages(self, actor, scope_id, query, policy, limit=5, *, exclude_citation_ids=()):
        return self.search(actor, scope_id, query, policy, limit,
                           exclude_citation_ids=exclude_citation_ids, granularity='page')

    def search_pipeline(self, actor, scope_id, query, policy, config=None, *, exclude_citation_ids=()):
        """Explicit configurable child fusion/rerank/parent pipeline.

        Model stages require an already published matching index and approved
        injected providers. No dependencies are downloaded and no vectors are
        silently built by this query API. Existing lexical search is unchanged.
        """
        from .retrieval_pipeline import execute

        return execute(self, actor, scope_id, query, policy, config,
                       exclude_citation_ids=exclude_citation_ids)

    def pipeline_capabilities(self, actor, scope_id, policy):
        """Read-only stage admission, without invoking an encoder or reranker."""
        from .retrieval_pipeline import _allowed_bindings

        policy = _policy(policy)
        state, bindings, build = self._state(actor, scope_id, with_build=True)
        allowed = _allowed_bindings(self, actor, bindings, policy)
        config = self.platform._build_config(build)
        provider = self.embedding_provider or getattr(self.platform, 'embedding_provider', None)
        dense_count = 0
        if 'dense' not in config['channels']:
            dense_reason = 'CHANNEL_NOT_ADMITTED'
        elif provider is None or provider.manifest != config.get('embedding'):
            dense_reason = 'MODEL_VERSION_MISMATCH'
        else:
            dense_count = len({row['source_id'] for row in allowed if row['policy'].get('embedding', False) and
                provider.provider_id in row['policy'].get('allowed_providers', []) and
                row['source_id'] in config.get('vector_source_ids', [])})
            dense_reason = None if dense_count else 'NO_AUTHORIZED_DENSE_SOURCES'
        reranker = self.pipeline_reranker or (getattr(self.research, 'luna', None) if self.research else None)
        rerank_count = len({row['source_id'] for row in allowed if reranker is not None and
            row['policy'].get('rerank', False) and
            reranker.provider_id in row['policy'].get('allowed_providers', [])})
        self._unchanged(actor, scope_id, state)
        return {'manifest_id': state['manifest_id'], 'channels': ['bm25'] + ([] if dense_reason else ['dense']),
                'dense_unavailable_reason': dense_reason, 'dense_eligible_source_count': dense_count,
                'reranker_configured': reranker is not None, 'rerank_eligible_source_count': rerank_count,
                'rerank_all_sources_admitted': bool(allowed) and rerank_count == len({row['source_id'] for row in allowed}),
                'parent_modes': ['none', 'paragraph', 'section', 'page'],
                'parent_admission': 'requires_real_source_structure; section/page_can_be_unavailable',
                'model_calls': 0}

    def search_tables(self, actor, scope_id, query, policy, limit=5, *, exclude_citation_ids=()):
        return self.search(actor, scope_id, query, policy, limit,
                           exclude_citation_ids=exclude_citation_ids, granularity='table')

    def search(self, actor, scope_id, query, policy, limit=5, *, exclude_citation_ids=(), granularity='block'):
        if granularity not in ('block', 'page', 'table'):
            raise PlatformError('INVALID_QUERY', 'Unknown retrieval granularity', 422)
        policy = _policy(policy)
        if not isinstance(exclude_citation_ids, (list, tuple)):
            raise PlatformError('INVALID_CITATIONS', 'Invalid excluded citation identifiers', 422)
        excluded = list(exclude_citation_ids)
        self._ids(excluded)
        if any(len(value) > 512 for value in excluded):
            raise PlatformError('INVALID_CITATIONS', 'Invalid excluded citation identifiers', 422)
        if (not isinstance(query, str) or not query.strip() or len(query) > 2000 or
                type(limit) is not int or not 1 <= limit <= 20):
            raise PlatformError('INVALID_QUERY', 'Invalid query or candidate limit', 422)
        state, bindings, build = self._state(actor, scope_id, with_build=True)
        allowed = self._allowed(actor, bindings, policy)
        if policy['version_policy'] == 'pinned':
            if any(row['source_id'] in policy['versions'] and
                   row['source_version_id'] != policy['versions'][row['source_id']] for row in allowed):
                raise PlatformError('PINNED_VERSION_NOT_INDEXED',
                                    'Use saved citations to read pinned historical evidence', 409)
            allowed = [row for row in allowed if policy['versions'].get(row['source_id']) ==
                       row['source_version_id']]
        groups = [allowed]
        if policy['mode'] == 'PREFER' and policy['source_ids']:
            groups = [[row for row in allowed if row['source_id'] in policy['source_ids']],
                      [row for row in allowed if row['source_id'] not in policy['source_ids']]]
        records = []
        try:
            for group in groups:
                if not group or len(records) >= limit:
                    continue
                candidates = self.platform.backend.search(build['index_name'], actor.tenant_id, scope_id,
                                                          group, query, limit - len(records),
                                                          **({'kind': 'Page' if granularity == 'page' else 'Table'}
                                                             if granularity in ('page', 'table') else {}),
                                                          **({'exclude_citation_ids': excluded} if excluded else {}))
                allowed_keys = {tuple(row[key] for key in BINDING_FIELDS) for row in group}
                if len(candidates) > limit - len(records):
                    raise PlatformError('INDEX_SCOPE_VIOLATION', 'Index exceeded the candidate budget', 503)
                for candidate in candidates:
                    if (candidate.get('tenant_id') != actor.tenant_id or candidate.get('kb_id') != scope_id or
                            candidate.get('build_id') != state['manifest_id'] or
                            candidate.get('citation_id') in excluded or
                            granularity == 'page' and candidate.get('kind') != 'Page' or
                            granularity == 'table' and candidate.get('kind') != 'Table' or
                            granularity == 'block' and candidate.get('kind') == 'Page' or
                            tuple(candidate.get(key) for key in BINDING_FIELDS) not in allowed_keys or
                            not isinstance(candidate.get('citation_id'), str) or
                            candidate.get('unit_id') != candidate['citation_id']):
                        raise PlatformError('INDEX_SCOPE_VIOLATION', 'Index result is outside the allowed sources', 503)
                    canonical = self._read_one(actor, scope_id, candidate['citation_id'], policy, state, bindings)
                    if granularity == 'table' and not canonical.get('table_data'):
                        raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Table index result has no typed original table', 503)
                    if any(candidate.get(key) != canonical[key] for key in
                           ('source_id', 'source_version_id', 'parse_snapshot_id', 'build_id',
                            'text', 'anchors', 'evidence_ids')):
                        raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Index differs from original evidence', 503)
                    score = candidate.get('score')
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                        raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Index score is invalid', 503)
                    if any(item['citation_id'] == canonical['citation_id'] for item in records):
                        raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Index returned a duplicate citation', 503)
                    records.append(evidence_record(canonical) |
                                   {'score': score, 'retrieval_method':
                                    'authorized_page_bm25' if granularity == 'page' else
                                    'authorized_table_bm25' if granularity == 'table' else RETRIEVAL_METHOD})
        except SearchUnavailable as exc:
            raise PlatformError('SEARCH_UNAVAILABLE', 'Search backend unavailable', 503) from exc
        self._unchanged(actor, scope_id, state)
        return records

    def navigate_references(self, actor, scope_id, citation_ids, policy, *, hops=2,
                            document_limit=16, fragment_limit=30):
        """Navigate written source titles/sections, delivering canonical evidence.

        Document mention edges are source navigation only. They are not trusted
        workflow next/branch transitions. Inaccessible targets are unresolved;
        titles and citations are drawn only from the authorized source set.
        """
        from .cross_document import navigate_source_references
        from .retrieval_pipeline import _allowed_bindings, _catalog

        policy = _policy(policy)
        self._ids(citation_ids)
        if (not citation_ids or len(citation_ids) > 3 or
                any(type(value) is not int for value in (hops, document_limit, fragment_limit)) or
                not 1 <= hops <= 4 or not 1 <= document_limit <= 32 or not 1 <= fragment_limit <= 50):
            raise PlatformError('INVALID_NAVIGATION', 'Invalid source seeds or navigation limits', 422)
        seeds = self.read(actor, scope_id, citation_ids, policy)
        state, bindings, _ = self._state(actor, scope_id, with_build=True)
        allowed = _allowed_bindings(self, actor, bindings, policy)
        documents, units = _catalog(self, actor, scope_id, state, allowed)
        by_source = {doc['source_id']: doc for doc in documents}
        if any(item['source_id'] not in by_source or item['build_id'] != state['manifest_id'] for item in seeds):
            raise PlatformError('SOURCE_VERSION_MISMATCH', 'Navigation requires current published evidence', 409)
        navigation = navigate_source_references(
            [{'id': doc['source_id'], 'title': doc['title'], 'text': doc['text']} for doc in documents],
            list(dict.fromkeys(item['source_id'] for item in seeds)), hops=hops,
            document_limit=document_limit, fragment_limit=fragment_limit)
        fragments, ids = [], []
        for fragment in navigation['fragments']:
            selected = sorted((unit for unit in units.values()
                if unit['source']['source_id'] == fragment['doc_id'] and unit['element']['kind'] != 'Page' and
                unit['start'] < fragment['end'] and unit['end'] > fragment['start']),
                key=lambda unit: (unit['start'], unit['end']))
            selected = [unit for unit in selected if unit['element']['kind'] != 'Table' or not any(
                other['element']['kind'] != 'Table' and other['start'] <= unit['start'] and
                other['end'] >= unit['end'] for other in selected)]
            additional = [unit['citation_id'] for unit in selected if unit['citation_id'] not in ids]
            if len(ids) + len(additional) > 50:
                raise PlatformError('NAVIGATION_BUDGET_EXCEEDED', 'Original fragments exceed the citation budget', 422)
            ids.extend(additional)
            fragments.append({'source_id': fragment['doc_id'], 'start': fragment['start'],
                'end': fragment['end'], 'text': fragment['text'], 'citation_anchors': [
                    {'citation_id': unit['citation_id'], 'start': max(fragment['start'], unit['start']) - unit['start'],
                     'end': min(fragment['end'], unit['end']) - unit['start'],
                     'text': by_source[fragment['doc_id']]['text'][max(fragment['start'], unit['start']):
                                                                  min(fragment['end'], unit['end'])]}
                    for unit in selected]})
        hits = self.read(actor, scope_id, ids, policy)
        by_citation = {item['citation_id']: item for item in hits}
        for fragment in fragments:
            if not fragment['citation_anchors'] or any(
                by_citation[anchor['citation_id']]['text'][anchor['start']:anchor['end']] != anchor['text']
                    for anchor in fragment['citation_anchors']):
                raise PlatformError('SOURCE_INTEGRITY', 'Navigation span differs from canonical evidence', 503)
        self._unchanged(actor, scope_id, state)
        return {'hits': hits, 'fragments': fragments, 'trace': {
            'method': 'explicit_original_source_references', 'model_calls': 0,
            'manifest_id': state['manifest_id'], 'visited_source_ids': navigation['visited_documents'],
            'unresolved_references': navigation['unresolved_references'],
            'observation_boundary': 'source_navigation; not_workflow_transition'}}

    def table(self, actor, scope_id, citation_id, policy, operation='rows', column=None, filters=None):
        policy = _policy(policy)
        self._ids([citation_id])
        state, bindings, _ = self._state(actor, scope_id)
        record = self._read_one(actor, scope_id, citation_id, policy, state, bindings)
        result = _table_query(record.get('table_data'), operation, column, filters)
        self._unchanged(actor, scope_id, state)
        return {**result, 'citation_id': citation_id, 'source_version_id': record['source_version_id'],
                'build_id': record['build_id'], 'anchors': record['anchors'],
                'evidence': evidence_record(record)}

    def workflow_next(self, actor, scope_id, citation_id, policy, *, current_step=None, facts=None):
        """Follow explicit typed source edges; unknown state requires clarification."""
        from .workflow_query import next_step

        policy = _policy(policy)
        self._ids([citation_id])
        state, bindings, _ = self._state(actor, scope_id)
        record = self._read_one(actor, scope_id, citation_id, policy, state, bindings)
        from .table_query import verified_table

        result = next_step(verified_table(record.get('table_data')), current_step, facts)
        self._unchanged(actor, scope_id, state)
        return {**result, 'citation_id': citation_id,
                'evidence': evidence_record(record),
                'trace': {'method': 'explicit_typed_workflow_edges/1', 'model_calls': 0,
                          'condition_semantics': 'exact_typed_equality',
                          'observation_boundary': 'source_supported_next_step; not_narrative_relation_inference'}}

    def table_join(self, actor, scope_id, citation_ids, policy, *, left_key, right_key,
                   how='inner', max_rows=100):
        """Join two authorized original typed tables, retaining both citations."""
        from .table_query import join_tables

        policy = _policy(policy)
        if (not isinstance(citation_ids, list) or len(citation_ids) != 2 or
                any(not isinstance(value, str) or not value for value in citation_ids)):
            raise PlatformError('INVALID_TABLE_JOIN', 'Provide exactly two original table citations', 422)
        self._ids(list(dict.fromkeys(citation_ids)))
        state, bindings, _ = self._state(actor, scope_id)
        records = [self._read_one(actor, scope_id, citation, policy, state, bindings) for citation in citation_ids]
        result = join_tables(records[0].get('table_data'), records[1].get('table_data'),
                             left_key=left_key, right_key=right_key, how=how, max_rows=max_rows)
        self._unchanged(actor, scope_id, state)
        return {**result, 'citation_ids': list(citation_ids),
                'evidence': [evidence_record(record) for record in records]}


def _number(value):
    if isinstance(value, bool) or value is None:
        raise PlatformError('NON_NUMERIC_CELL', 'Numeric operation requires verified values', 422)
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (DecimalException, ValueError) as exc:
        raise PlatformError('NON_NUMERIC_CELL', 'Numeric operation requires verified values', 422) from exc


def _table_query(table, operation, column, filters):
    if not isinstance(table, dict) or not table or operation not in ('rows', 'count', 'sum', 'min', 'max'):
        raise PlatformError('INVALID_TABLE_QUERY', 'Unsupported table operation', 422)
    filters = [] if filters is None else filters
    if not isinstance(filters, list) or len(filters) > 10:
        raise PlatformError('INVALID_TABLE_QUERY', 'At most 10 table filters are permitted', 422)
    from .table_query import verified_table

    verified = verified_table(table)
    headers, rows = verified['headers'], verified['rows']
    indexed_rows = list(zip(verified['source_row_numbers'], rows, strict=True))
    for item in filters:
        if (not isinstance(item, dict) or set(item) - {'column', 'op', 'value'} or
                item.get('column') not in headers or item.get('op') not in ('eq', 'lt', 'lte', 'gt', 'gte')):
            raise PlatformError('INVALID_TABLE_QUERY', 'Unsupported table filter', 422)
        index, op, value = headers.index(item['column']), item['op'], item.get('value')
        selected = []
        for row_number, row in indexed_rows:
            cell = row[index] if index < len(row) else None
            if op == 'eq':
                matches = cell == value
            else:
                left, right = _number(cell), _number(value)
                matches = {'lt': left < right, 'lte': left <= right, 'gt': left > right, 'gte': left >= right}[op]
            if matches:
                selected.append((row_number, row))
        indexed_rows = selected
    rows = [row for _, row in indexed_rows]
    if operation == 'rows':
        result = rows[:1000]
    elif operation == 'count':
        result = len(rows)
    else:
        if column not in headers:
            raise PlatformError('INVALID_TABLE_QUERY', 'Unknown table column', 422)
        index = headers.index(column)
        values = [_number(row[index] if index < len(row) else None) for row in rows]
        if not values:
            raise PlatformError('NO_NUMERIC_VALUES', 'No numeric values in the selected range', 422)
        result = str(sum(values) if operation == 'sum' else min(values) if operation == 'min' else max(values))
    formula_by_row = dict(zip(verified['source_row_numbers'], verified['cached_formula_cells'], strict=True))
    return {'operation': operation, 'result': result, 'headers': headers, 'matched_rows': len(rows),
            'source_row_numbers': [number for number, _ in indexed_rows[:1000]],
            'cached_formula_cells': [cell for number, _ in indexed_rows[:1000] for cell in formula_by_row[number]],
            'row_lineage_truncated': len(indexed_rows) > 1000,
            'truncated': operation == 'rows' and len(rows) > 1000,
            'complete_result': operation != 'rows' or len(rows) <= 1000}


def bounded_calculate(expression):
    """Evaluate decimal arithmetic only; no names, calls, containers or attributes."""
    def invalid():
        return PlatformError('INVALID_EXPRESSION', 'Use bounded decimal arithmetic only', 422)

    if not isinstance(expression, str) or not expression.strip() or len(expression) > 512:
        raise invalid()
    try:
        tree = ast.parse(expression.strip(), mode='eval')
        if sum(1 for _ in ast.walk(tree)) > 128:
            raise invalid()

        def evaluate(node, depth=0):
            if depth > 16:
                raise invalid()
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                value = Decimal(ast.get_source_segment(expression.strip(), node).replace('_', ''))
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = evaluate(node.operand, depth + 1)
                value = value if isinstance(node.op, ast.UAdd) else -value
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div,
                                                                    ast.FloorDiv, ast.Mod, ast.Pow)):
                left, right = evaluate(node.left, depth + 1), evaluate(node.right, depth + 1)
                if isinstance(node.op, ast.Add):
                    value = left + right
                elif isinstance(node.op, ast.Sub):
                    value = left - right
                elif isinstance(node.op, ast.Mult):
                    value = left * right
                elif isinstance(node.op, ast.Div):
                    value = left / right
                elif isinstance(node.op, ast.FloorDiv):
                    value = left // right
                    if left % right and (left < 0) != (right < 0):
                        value -= 1
                elif isinstance(node.op, ast.Mod):
                    value = left % right
                    if value and (left < 0) != (right < 0):
                        value += right
                else:
                    if right != right.to_integral_value() or abs(right) > 32:
                        raise invalid()
                    value = left ** int(right)
            else:
                raise invalid()
            if not value.is_finite() or value and not -100 <= value.adjusted() <= 100:
                raise invalid()
            return value

        with localcontext() as context:
            context.prec, context.Emax, context.Emin = 50, 100, -100
            context.traps[Underflow] = context.traps[Subnormal] = True
            return evaluate(tree.body)
    except (SyntaxError, ValueError, DecimalException, RecursionError) as exc:
        raise invalid() from exc

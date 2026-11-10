from __future__ import annotations

from .knowledge_queries import KnowledgeQuery
from .knowledge_embedding import embedding_audit
from .knowledge_parse_cache import ImmutableParseCache

from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL


from .session_identity import Actor as Actor, PlatformError as PlatformError

from .search import SearchGenerationIncomplete, SearchUnavailable, TIE_BREAK_FIELDS, content_order, QUERY_PROTOCOL
from .embeddings import EmbeddingError


CAPABILITIES = {'kb.read', 'kb.write', 'kb.manage', 'kb.manage_members', 'kb.manage_managers',
                'kb.delete', 'kb.audit'}
DEFAULT_POLICY = {'native_parse': True, 'ocr': False, 'embedding': False, 'rerank': False,
                  'llm_context': False, 'compile': False, 'preview': True, 'download': True,
                  'allowed_providers': [], 'include_hidden': False, 'include_notes': False}


def _deny():
    raise PlatformError('ACCESS_DENIED', 'Resource unavailable or access denied', 403)


def _policy(value: dict | None) -> dict:
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - DEFAULT_POLICY.keys():
        raise PlatformError('INVALID_POLICY', 'Unknown processing policy')
    result = {**DEFAULT_POLICY, **value}
    if any(type(v) is not bool for k, v in result.items() if k != 'allowed_providers'):
        raise PlatformError('INVALID_POLICY', 'Policy flags must be boolean')
    providers = result['allowed_providers']
    if not isinstance(providers, list) or any(not isinstance(p, str) for p in providers):
        raise PlatformError('INVALID_POLICY', 'Provider list must contain strings')
    return result


class Platform:
    def __init__(self, *, repository, search, store, parser, embedding_jobs,
                 embedding_provider=None, ocr_client=None):
        self.repository = repository
        self.backend, self.store, self.parser = search, store, parser
        self.embedding_jobs = embedding_jobs
        self.embedding_provider, self.ocr_client = embedding_provider, ocr_client
        self._parse_cache = ImmutableParseCache()

    def _tx(self, actor: Actor, kb_id: str | None = None):
        return self.repository.transaction(actor, kb_id)

    def _auth(self, conn, actor: Actor, kb_id: str, capability='kb.read') -> dict:
        if not kb_id:
            _deny()
        row = conn.execute(KnowledgeQuery.KB_MEMBERSHIP,
                           (actor.tenant_id, kb_id, actor.principal_id)).fetchone()
        if not row or capability not in row['capabilities']:
            _deny()
        return row

    def _lock_authority(self, conn, actor: Actor) -> int:
        row = conn.execute(KnowledgeQuery.AUTHORITY_LOCK,
                           (actor.tenant_id,)).fetchone()
        if not row:
            _deny()
        return row['revision']

    def _revision(self, conn, actor: Actor) -> int:
        row = conn.execute(KnowledgeQuery.AUTHORITY_REVISION,
                           (actor.tenant_id,)).fetchone()
        if not row:
            _deny()
        return row['revision']

    def _bump(self, conn, actor: Actor, kb_id: str | None = None):
        conn.execute(KnowledgeQuery.AUTHORITY_BUMP, (actor.tenant_id,))
        if kb_id:
            conn.execute(KnowledgeQuery.KB_BUMP,
                         (actor.tenant_id, kb_id))

    def _source(self, conn, actor: Actor, source_id: str, owner=False) -> dict:
        row = conn.execute(KnowledgeQuery.SOURCE_GET,
                           (actor.tenant_id, source_id)).fetchone()
        if not row or (row['owner_id'] != actor.principal_id and
                       (owner or actor.principal_id not in row['readers'])):
            _deny()
        return row

    def create_kb(self, actor: Actor, name: str) -> dict:
        if not name.strip() or len(name) > 200:
            raise PlatformError('INVALID_NAME', 'Knowledge base name is required')
        kb_id = str(uuid4())
        with self._tx(actor, kb_id) as conn:
            conn.execute(KnowledgeQuery.AUTHORITY_ENSURE,
                         (actor.tenant_id,))
            self._lock_authority(conn, actor)
            conn.execute(KnowledgeQuery.KB_INSERT,
                         (actor.tenant_id, kb_id, name, actor.principal_id))
            conn.execute(KnowledgeQuery.MEMBER_INSERT,
                         (actor.tenant_id, kb_id, actor.principal_id, sorted(CAPABILITIES)))
            self._bump(conn, actor)
        return {'kb_id': kb_id, 'name': name, 'tenant_id': actor.tenant_id, 'manifest_id': None}

    def list_kbs(self, actor: Actor) -> list[dict]:
        with self._tx(actor) as conn:
            return conn.execute(KnowledgeQuery.KBS_VISIBLE,
                                (actor.tenant_id, actor.principal_id)).fetchall()

    def set_member(self, actor: Actor, kb_id: str, principal_id: str, capabilities: list[str]) -> dict:
        if not principal_id or set(capabilities) - CAPABILITIES:
            raise PlatformError('INVALID_CAPABILITIES', 'Invalid member capabilities')
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage_members')
            previous = conn.execute(KnowledgeQuery.MEMBER_GET, (actor.tenant_id, kb_id, principal_id)).fetchone()
            management = {'kb.manage', 'kb.manage_members', 'kb.manage_managers', 'kb.delete'}
            affected = set(capabilities) | set(previous['capabilities'] if previous else [])
            if (not set(capabilities).issubset(kb['capabilities']) or
                (affected & management and 'kb.manage_managers' not in kb['capabilities'])):
                _deny()
            if principal_id == kb['owner_id'] and set(capabilities) != CAPABILITIES:
                raise PlatformError('OWNER_REQUIRED', 'Owner capabilities cannot be removed', 409)
            conn.execute(KnowledgeQuery.MEMBER_UPSERT,
                         (actor.tenant_id, kb_id, principal_id, sorted(set(capabilities))))
            self._bump(conn, actor, kb_id)
        return {'principal_id': principal_id, 'capabilities': sorted(set(capabilities))}

    def ingest(self, actor: Actor, kb_id: str, filename: str, data: bytes,
               readers: list[str] | None = None, policy: dict | None = None,
               source_id: str | None = None, expected_revision: int | None = None) -> dict:
        from .documents import max_upload_bytes, processing_flags

        limit = max_upload_bytes()
        if not data or len(data) > limit:
            raise PlatformError('FILE_SIZE', f'File must contain 1 to {limit} bytes', 422)
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        if not filename or len(filename) > 255:
            raise PlatformError('INVALID_FILENAME', 'Invalid filename', 422)
        with self._tx(actor, kb_id) as conn:
            self._auth(conn, actor, kb_id, 'kb.write')
            self._expect_revision(conn, actor, expected_revision)
            existing = self._source(conn, actor, source_id, owner=True) if source_id else None
            parse_policy = _policy(policy if policy is not None else existing['policy'] if existing else None)
            parse_authorization = {'principal': f'{actor.tenant_id}:{actor.principal_id}',
                                   'acl_revision': str(self._revision(conn, actor))}
        path, digest = self.store.put(actor.tenant_id, data)
        document = self.parser(data, filename, {**parse_policy, '_parse_authorization': parse_authorization})
        if (self.ocr_client is not None and parse_policy.get('ocr') and
                self.ocr_client.provider_id in parse_policy['allowed_providers'] and
                document['status'] in {'PARTIAL', 'BLOCKED'}):
            from .ocr import augment_ocr

            document = augment_ocr(data, filename, document, self.ocr_client)
        document['processing_policy'] = processing_flags(parse_policy)
        if document.get('original_sha256') != digest or document.get('source_version_id') != digest:
            raise PlatformError('SOURCE_INTEGRITY', 'Parser source identity mismatch', 422)
        source_id = source_id or str(uuid4())
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            self._expect_revision(conn, actor, expected_revision)
            if existing:
                current = self._source(conn, actor, source_id, owner=True)
                if current['revision'] != existing['revision']:
                    raise PlatformError('STALE_PARSE', 'Source policy changed during parsing', 409)
                effective_readers = readers if readers is not None else current['readers']
                conn.execute(KnowledgeQuery.SOURCE_UPDATE,
                             (effective_readers, parse_policy, actor.tenant_id, source_id))
            else:
                conn.execute(KnowledgeQuery.SOURCE_INSERT,
                             (actor.tenant_id, source_id, actor.principal_id, readers or [], parse_policy))
            conn.execute(KnowledgeQuery.SOURCE_VERSION_INSERT,
                         (actor.tenant_id, source_id, digest, filename, path, digest, document['media_type']))
            conn.execute(KnowledgeQuery.PARSE_INSERT,
                         (actor.tenant_id, source_id, digest, document['parse_snapshot_id'], document))
            binding = self._bind(conn, actor, kb_id, source_id, digest, document['parse_snapshot_id'])
            self._bump(conn, actor, kb_id)
        return {**binding, 'document': document}

    def _bind(self, conn, actor, kb_id, source_id, source_version_id, parse_id):
        row = conn.execute(KnowledgeQuery.BINDING_UPSERT,
                           (actor.tenant_id, kb_id, str(uuid4()), source_id, source_version_id, parse_id)).fetchone()
        return row

    def bind_source(self, actor, kb_id, source_id, source_version_id, expected_revision=None):
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            self._expect_revision(conn, actor, expected_revision)
            self._source(conn, actor, source_id, owner=True)
            snapshot = conn.execute(KnowledgeQuery.PARSE_FIRST,
                                    (actor.tenant_id, source_id, source_version_id)).fetchone()
            if not snapshot:
                _deny()
            result = self._bind(conn, actor, kb_id, source_id, source_version_id, snapshot['parse_snapshot_id'])
            self._bump(conn, actor, kb_id)
            return result

    def remove_binding(self, actor, kb_id, binding_id, expected_revision=None):
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            self._expect_revision(conn, actor, expected_revision)
            row = conn.execute(KnowledgeQuery.BINDING_REMOVE,
                               (actor.tenant_id, kb_id, binding_id)).fetchone()
            if not row:
                _deny()
            self._bump(conn, actor, kb_id)
            return row

    def set_source_readers(self, actor, source_id, readers, expected_revision=None):
        if not isinstance(readers, list) or any(not isinstance(p, str) or not p for p in readers):
            raise PlatformError('INVALID_READERS', 'Readers must be principal identifiers')
        with self._tx(actor) as conn:
            self._lock_authority(conn, actor)
            self._source(conn, actor, source_id, owner=True)
            self._expect_revision(conn, actor, expected_revision)
            conn.execute(KnowledgeQuery.SOURCE_READERS_UPDATE,
                         (sorted(set(readers)), actor.tenant_id, source_id))
            self._bump(conn, actor)
        return {'source_id': source_id, 'readers': sorted(set(readers))}

    def set_source_policy(self, actor, source_id, policy, expected_revision=None):
        policy = _policy(policy)
        with self._tx(actor) as conn:
            self._lock_authority(conn, actor)
            self._source(conn, actor, source_id, owner=True)
            self._expect_revision(conn, actor, expected_revision)
            conn.execute(KnowledgeQuery.SOURCE_POLICY_UPDATE,
                         (policy, actor.tenant_id, source_id))
            self._bump(conn, actor)
        return {'source_id': source_id, 'policy': policy}

    def _expect_revision(self, conn, actor, expected_revision):
        if expected_revision is not None:
            if type(expected_revision) is not int or expected_revision < 0:
                raise PlatformError('INVALID_REVISION', 'Revision must be a nonnegative integer', 422)
            if self._lock_authority(conn, actor) != expected_revision:
                raise PlatformError('AUTHORITY_REVISION_CONFLICT', 'Reload current authority before changing documents', 409)

    def _source_snapshot(self, actor, kb_id, *, processing=False, capability='kb.read', published=False):
        with self._tx(actor, kb_id) as conn:
            revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, capability)
            self._auth(conn, actor, kb_id)
            if processing:
                rows = conn.execute(KnowledgeQuery.BINDINGS_PROCESSING, (actor.tenant_id, kb_id)).fetchall()
            else:
                rows = conn.execute(KnowledgeQuery.BINDINGS_ACTIVE, (actor.tenant_id, kb_id)).fetchall()
            if published:
                build = conn.execute(KnowledgeQuery.BUILD_PUBLISHED,
                    (actor.tenant_id, kb_id, kb['manifest_id'])).fetchone() if kb['manifest_id'] else None
                selected = {} if build is None else {item['source_id']: item for item in build['inputs']['sources']}
                current = []
                for row in rows:
                    original = selected.get(row['source_id'])
                    if original is None or original['binding_id'] != row['binding_id']:
                        continue
                    version = conn.execute(KnowledgeQuery.SOURCE_VERSION_INFO,
                        (actor.tenant_id, row['source_id'], original['source_version_id'])).fetchone()
                    if version is None:
                        raise PlatformError('SOURCE_INTEGRITY', 'Published source version is unavailable', 503)
                    current.append({**row, **{key: original[key] for key in
                        ('source_version_id', 'parse_snapshot_id')}, 'filename': version['filename']})
                rows = current
        documents, missing = {}, []
        for row in rows:
            identity = (row['source_id'], row['source_version_id'], row['parse_snapshot_id'])
            key = (self.repository.identity.dataset_id, actor.tenant_id, *identity)
            document = self._parse_cache.get(key)
            if document is None:
                missing.append(identity)
            else:
                documents[identity] = document
        for identity, document in self.repository.parse_documents(actor, kb_id, missing):
            documents[identity] = document
            self._parse_cache.put((self.repository.identity.dataset_id, actor.tenant_id, *identity), document)
        hydrated = [{**row, 'document': documents[(row['source_id'], row['source_version_id'],
                                                  row['parse_snapshot_id'])]} for row in rows]
        snapshot = {'actor': actor, 'scope': kb_id, 'kb': kb, 'authority_revision': revision,
                    'capability': capability, 'rows': hydrated}
        with self._tx(actor, kb_id) as conn:
            self._check_source_snapshot(conn, actor, kb_id, snapshot)
        return snapshot

    def documents(self, actor, kb_id, provider_id=None, include_removed=False):
        if provider_id is not None and (not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 200):
            raise PlatformError('INVALID_PROVIDER', 'Provider identity is invalid', 422)
        if type(include_removed) is not bool:
            raise PlatformError('INVALID_DOCUMENT_QUERY', 'include_removed must be boolean', 422)
        with self._tx(actor, kb_id) as conn:
            revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id)
            rows = conn.execute(KnowledgeQuery.BINDINGS_DOCUMENTS, (actor.tenant_id, kb_id)).fetchall()
            rows = [row for row in rows if (include_removed or row['status'] == 'ACTIVE') and
                (row['owner_id'] == actor.principal_id or actor.principal_id in row['readers'])]
            build = conn.execute(KnowledgeQuery.BUILD_PUBLISHED,
                (actor.tenant_id, kb_id, kb['manifest_id'])).fetchone() if kb['manifest_id'] else None
        published = {} if build is None else {item['source_id']: item for item in build['inputs']['sources']}
        identities = list(dict.fromkeys((row['source_id'], row['source_version_id'], row['parse_snapshot_id'])
            for row in rows))
        for row in rows:
            previous = published.get(row['source_id'])
            if previous is not None:
                identity = (row['source_id'], previous['source_version_id'], previous['parse_snapshot_id'])
                if identity not in identities:
                    identities.append(identity)
        parsed = dict(self.repository.parse_documents(actor, kb_id, identities))
        documents = []
        for row in rows:
            document = parsed[(row['source_id'], row['source_version_id'], row['parse_snapshot_id'])]
            previous = published.get(row['source_id'])
            published_document = (parsed[(row['source_id'], previous['source_version_id'], previous['parse_snapshot_id'])]
                if previous is not None else None)
            reasons = []
            if row['status'] != 'ACTIVE':
                reasons.append('BINDING_REMOVED')
            if previous is None:
                reasons.append('NOT_PUBLISHED')
            if not row['policy']['llm_context']:
                reasons.append('LLM_CONTEXT_DENIED')
            if provider_id is None:
                reasons.append('MODEL_NOT_CONFIGURED')
            elif provider_id not in row['policy']['allowed_providers']:
                reasons.append('PROVIDER_NOT_ALLOWED')
            if (published_document is not None and row['policy']['llm_context']
                    and not self._allowed([{**row, 'document': published_document}], actor, 'llm_context')):
                reasons.append('PROCESSING_POLICY_DENIED')
            build_blockers = [] if document['status'] == 'READY' else ['INCOMPLETE_PARSE']
            if not row['policy']['native_parse']:
                build_blockers.append('PROCESSING_DENIED')
            documents.append({'source_id': row['source_id'], 'binding_id': row['binding_id'],
                'filename': row['filename'], 'source_version_id': row['source_version_id'],
                'parse_snapshot_id': row['parse_snapshot_id'], 'parse_status': document['status'],
                'binding_status': row['status'], 'source_revision': row['source_revision'],
                'binding_revision': row['revision'], 'policy': row['policy'],
                'published_version_id': None if previous is None else previous['source_version_id'],
                'published_parse_snapshot_id': None if previous is None else previous['parse_snapshot_id'],
                'needs_publication': previous is None or any(row[key] != previous[key] for key in
                    ('source_version_id', 'parse_snapshot_id')) or row['status'] != 'ACTIVE',
                'human_search_ready': previous is not None and row['status'] == 'ACTIVE' and
                    bool(self._allowed([{**row, 'document': published_document}], actor, 'human_search')),
                'qa_ready': not reasons, 'blockers': list(dict.fromkeys(reasons)),
                'build_blockers': build_blockers, 'quality_report': document.get('quality_report', {})})
        with self._tx(actor, kb_id) as conn:
            self._auth(conn, actor, kb_id)
            if self._lock_authority(conn, actor) != revision:
                raise PlatformError('SCOPE_CHANGED', 'Documents changed during diagnostic read', 409)
        return {'kb_id': kb_id, 'authority_revision': revision, 'manifest_id': kb['manifest_id'],
            'provider_id': provider_id, 'documents': documents}

    def source_versions(self, actor, kb_id, source_id):
        with self._tx(actor, kb_id) as conn:
            self._auth(conn, actor, kb_id)
            self._source(conn, actor, source_id)
            binding = conn.execute(KnowledgeQuery.BINDING_ACCESS, (actor.tenant_id, kb_id, source_id)).fetchone()
            if binding is None:
                _deny()
            versions = conn.execute(KnowledgeQuery.SOURCE_VERSIONS_LIST, (actor.tenant_id, source_id)).fetchall()
        return {'source_id': source_id, 'versions': [dict(row) for row in versions]}

    def reparse_source(self, actor, kb_id, source_id, expected_revision=None):
        with self._tx(actor, kb_id) as conn:
            self._auth(conn, actor, kb_id, 'kb.write')
            self._source(conn, actor, source_id, owner=True)
            self._expect_revision(conn, actor, expected_revision)
            revision = self._lock_authority(conn, actor)
            binding = conn.execute(KnowledgeQuery.BINDING_ACCESS, (actor.tenant_id, kb_id, source_id)).fetchone()
            if binding is None:
                _deny()
            identity = (actor.tenant_id, source_id, binding['source_version_id'])
            original = conn.execute(KnowledgeQuery.SOURCE_VERSION_ORIGINAL, identity).fetchone()
            metadata = conn.execute(KnowledgeQuery.SOURCE_VERSION_INFO, identity).fetchone()
        if original is None or metadata is None:
            raise PlatformError('SOURCE_INTEGRITY', 'Original source version is unavailable', 503)
        data = self.store.read(original['original_path'], actor.tenant_id, original['original_sha256'])
        return self.ingest(actor, kb_id, metadata['filename'], data, source_id=source_id,
            expected_revision=revision)

    def _check_source_snapshot(self, conn, actor, kb_id, snapshot):
        if snapshot['actor'] != actor or snapshot['scope'] != kb_id:
            _deny()
        revision = self._lock_authority(conn, actor)
        kb = self._auth(conn, actor, kb_id, snapshot['capability'])
        self._auth(conn, actor, kb_id)
        if (revision != snapshot['authority_revision'] or kb['revision'] != snapshot['kb']['revision']
                or kb['manifest_id'] != snapshot['kb']['manifest_id']):
            raise PlatformError('SCOPE_CHANGED', 'Sources or authorization changed during snapshot read', 409)
        return kb

    def _allowed(self, rows, actor, purpose):
        if purpose not in {'human_search', 'llm_context', 'preview', 'download'}:
            raise PlatformError('INVALID_PURPOSE', 'Unknown access purpose')
        return [row for row in rows if
                (row['owner_id'] == actor.principal_id or actor.principal_id in row['readers']) and
                not any(row.get('document', {}).get('processing_policy', {}).get(flag, False)
                        and not row['policy'].get(flag, False) for flag in ('include_hidden', 'include_notes', 'ocr')) and
                (purpose == 'human_search' or row['policy'].get(purpose, False))]

    def _store_citations(self, actor, kb_id, build_id, snapshot, citations):
        def authorize(conn):
            kb = self._check_source_snapshot(conn, actor, kb_id, snapshot)
            build = conn.execute(KnowledgeQuery.BUILD_GET, (actor.tenant_id, kb_id, build_id)).fetchone()
            if (not build or build['status'] != 'STAGING'
                    or build['authority_revision'] != snapshot['authority_revision']
                    or build['kb_revision'] != kb['revision']
                    or build['expected_manifest_id'] != kb['manifest_id']):
                raise PlatformError('STALE_BUILD', 'Build changed during citation population', 409)
        self.repository.write_citations(actor, kb_id, citations, authorize)

    def stage(self, actor, kb_id, channels=None):
        from hashlib import sha256
        from . import delivery_units, retrieval_expansion, search, evidence_packing

        channels = channels if channels is not None else ['bm25', 'structure', 'table']
        if (not isinstance(channels, list) or not channels or
                any(channel not in {'bm25', 'structure', 'table', 'dense', 'hybrid'} for channel in channels)):
            raise PlatformError('CHANNEL_NOT_ADMITTED', 'Unknown requested channel', 409)
        model_enabled = bool(set(channels) & {'dense', 'hybrid'})
        channels = sorted(set(channels) | {'bm25', 'structure', 'table'} | ({'dense'} if model_enabled else set()))
        if model_enabled and self.embedding_provider is None:
            raise PlatformError('CHANNEL_NOT_ADMITTED', 'No approved configured embedding provider', 409)
        build_id = str(uuid4())
        index = self.backend.index_name(actor.tenant_id, kb_id, build_id)
        snapshot = self._source_snapshot(actor, kb_id, capability='kb.manage')
        rows = snapshot['rows']
        if len(self._allowed(rows, actor, 'human_search')) != len(rows):
            _deny()
        if not rows:
            raise PlatformError('NO_SOURCES', 'No active sources to build', 409)
        if any(row['document']['status'] != 'READY' for row in rows):
            raise PlatformError('INCOMPLETE_PARSE', 'All sources must have complete READY parses', 409)
        if any(not row['policy'].get('native_parse', False) for row in rows):
            raise PlatformError('PROCESSING_DENIED', 'Processing policy blocks this build', 403)
        units, citations = self._units(actor, kb_id, build_id, rows)
        if not units:
            raise PlatformError('NO_EVIDENCE', 'No indexed source evidence', 409)
        inputs = [{key: row[key] for key in ('binding_id','source_id','source_version_id',
                                              'parse_snapshot_id','revision','source_revision')} for row in rows]
        vector_rows = [row for row in rows if self._embedding_allowed(row)] if model_enabled else []
        if model_enabled and not vector_rows:
            raise PlatformError('PROCESSING_DENIED', 'No sources permit this embedding provider', 403)
        vector_sources = {row['source_id'] for row in vector_rows}
        config = {'sources': inputs, 'channels': channels,
                  'retrieval_unit_protocol': delivery_units.PROTOCOL,
                  'page_navigation_protocol': delivery_units.PAGE_PROTOCOL,
                  'retrieval_unit_implementation_sha256': sha256(Path(delivery_units.__file__).read_bytes()).hexdigest(),
                  'retrieval_chunking_implementation_sha256': sha256(Path(retrieval_expansion.__file__).read_bytes()).hexdigest(),
                  'lexical_query_protocol': search.QUERY_PROTOCOL,
                  'lexical_query_implementation_sha256': sha256(Path(search.__file__).read_bytes()).hexdigest(),
                  'search_backend': type(self.backend).__name__,
                  'search_dependencies': search.backend_dependencies(self.backend),
                  'query_blocks_implementation_sha256': sha256(Path(evidence_packing.__file__).read_bytes()).hexdigest(),
                  'tie_break': list(TIE_BREAK_FIELDS),
                  'embedding': self.embedding_provider.manifest if model_enabled else None,
                  'vector_source_ids': sorted(vector_sources),
                  'excluded_vector_source_ids': sorted({row['source_id'] for row in rows} - vector_sources)
                  if model_enabled else [],
                  'model_admission': 'EXPERIMENTAL' if model_enabled else 'NOT_ENABLED'}
        with self._tx(actor, kb_id) as conn:
            self._check_source_snapshot(conn, actor, kb_id, snapshot)
            authority_revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage')
            self._auth(conn, actor, kb_id, 'kb.read')
            conn.execute(KnowledgeQuery.BUILD_INSERT,
                         (actor.tenant_id, kb_id, build_id, kb['manifest_id'], authority_revision,
                          kb['revision'], index, config, len(units)))
        if getattr(self.backend, 'supports_resume_build', type(self.backend).__name__ == 'PostgresSearch'):
            return self.resume_build(actor, kb_id, build_id)
        self._store_citations(actor, kb_id, build_id, snapshot, citations)
        try:
            if model_enabled:
                eligible = [unit for unit in units if unit['source_id'] in vector_sources and unit['kind'] != 'Page']
                provider = self.embedding_provider
                if getattr(provider, 'requires_authorization', False):
                    def authorize():
                        with self._tx(actor, kb_id) as conn:
                            if self._lock_authority(conn, actor) != authority_revision:
                                raise PlatformError('STALE_BUILD', 'Authorization changed before model processing', 409)
                            current = self._auth(conn, actor, kb_id, 'kb.manage')
                            if current['revision'] != kb['revision'] or current['manifest_id'] != kb['manifest_id']:
                                raise PlatformError('STALE_BUILD', 'Knowledge base changed during model processing', 409)
                        return True

                    audit = embedding_audit(actor, kb_id, build_id, authority_revision, rows)
                    vectors, size = [], provider.max_batch_size
                    for start in range(0, len(eligible), size):
                        vectors.extend(provider.documents([unit['text'] for unit in eligible[start:start + size]],
                            request_id=f'build-{build_id}-{start}', audit_context=audit, authorize=authorize))
                else:
                    with self._tx(actor, kb_id) as conn:
                        if self._lock_authority(conn, actor) != authority_revision:
                            raise PlatformError('STALE_BUILD', 'Authorization changed before model processing', 409)
                        self._auth(conn, actor, kb_id, 'kb.manage')
                        vectors = provider.documents([unit['text'] for unit in eligible])
                for unit, vector in zip(eligible, vectors, strict=True):
                    unit['vector'] = vector
                self.backend.stage(index, units, vector_dimension=self.embedding_provider.dimension)
            else:
                self.backend.stage(index, units)
        except (SearchUnavailable, EmbeddingError, PlatformError) as exc:
            with self._tx(actor, kb_id) as conn:
                conn.execute(KnowledgeQuery.BUILD_FAIL,
                             (actor.tenant_id, kb_id, build_id))
            if isinstance(exc, PlatformError):
                raise
            if isinstance(exc, EmbeddingError):
                raise PlatformError('EMBEDDING_FAILED', str(exc), 422) from exc
            raise PlatformError('SEARCH_UNAVAILABLE', str(exc), 503) from exc
        with self._tx(actor, kb_id) as conn:
            conn.execute(KnowledgeQuery.BUILD_VALIDATE,
                         (actor.tenant_id, kb_id, build_id))
        return {'build_id': build_id, 'kb_id': kb_id, 'status': 'VALIDATED', 'unit_count': len(units),
                'channels': channels, 'expected_manifest_id': kb['manifest_id'],
                'embedding': config['embedding'], 'model_admission': config['model_admission'],
                'tie_break': config['tie_break'],
                'retrieval_unit_protocol': config['retrieval_unit_protocol'],
                'retrieval_unit_implementation_sha256': config['retrieval_unit_implementation_sha256'],
                'lexical_query_protocol': config['lexical_query_protocol'],
                'lexical_query_implementation_sha256': config['lexical_query_implementation_sha256'],
                'excluded_vector_source_count': len(config['excluded_vector_source_ids'])}

    def build_status(self, actor, kb_id, build_id):
        with self._tx(actor, kb_id) as conn:
            self._auth(conn, actor, kb_id, 'kb.manage')
            row = conn.execute(KnowledgeQuery.BUILD_STATUS,
                               (actor.tenant_id, kb_id, build_id)).fetchone()
            if row is None:
                _deny()
        return {**row, 'embedding_batches': self.embedding_jobs.status(actor.tenant_id, kb_id, build_id)}

    def resume_build(self, actor, kb_id, build_id):
        from hashlib import sha256
        from .embedding_jobs import EmbeddingJobError
        from .search import backend_dependencies
        from . import delivery_units, retrieval_expansion

        with self._tx(actor, kb_id) as conn:
            revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage')
            self._auth(conn, actor, kb_id, 'kb.read')
            build = conn.execute(KnowledgeQuery.BUILD_GET,
                                 (actor.tenant_id, kb_id, build_id)).fetchone()
            if build is None:
                _deny()
            if build['status'] in ('VALIDATED', 'PUBLISHED'):
                return {'build_id': build_id, 'kb_id': kb_id, 'status': build['status'], 'unit_count': build['unit_count']}
            config = build['inputs']
            if (build['status'] != 'STAGING' or build['authority_revision'] != revision or
                    build['kb_revision'] != kb['revision'] or build['expected_manifest_id'] != kb['manifest_id']):
                raise PlatformError('STALE_BUILD', 'Build inputs or authority changed', 409)
            if config['search_dependencies'] != backend_dependencies(self.backend):
                raise PlatformError('STALE_BUILD', 'Search implementation changed', 409)
            if (config['retrieval_unit_protocol'] != delivery_units.PROTOCOL or
                    config['retrieval_unit_implementation_sha256'] != sha256(Path(delivery_units.__file__).read_bytes()).hexdigest() or
                    config['retrieval_chunking_implementation_sha256'] != sha256(Path(retrieval_expansion.__file__).read_bytes()).hexdigest()):
                raise PlatformError('STALE_BUILD', 'Frozen chunking implementation changed', 409)
        snapshot = self._source_snapshot(actor, kb_id, capability='kb.manage')
        if (snapshot['authority_revision'] != revision or snapshot['kb']['revision'] != kb['revision']
                or snapshot['kb']['manifest_id'] != kb['manifest_id']):
            raise PlatformError('STALE_BUILD', 'Build inputs changed before source hydration', 409)
        rows = snapshot['rows']
        if len(self._allowed(rows, actor, 'human_search')) != len(rows):
            _deny()
        inputs = [{key: row[key] for key in ('binding_id', 'source_id', 'source_version_id',
                   'parse_snapshot_id', 'revision', 'source_revision')} for row in rows]
        if inputs != config['sources']:
            raise PlatformError('STALE_BUILD', 'Frozen source dependencies changed', 409)
        units, generated = self._units(actor, kb_id, build_id, rows)
        fields = ('tenant_id', 'kb_id', 'citation_id', 'build_id', 'binding_id', 'source_id',
                  'source_version_id', 'parse_snapshot_id', 'element_id', 'text', 'evidence_ids', 'anchors', 'table_data')
        expected = {r[2]: dict(zip(fields, r, strict=True)) for r in generated}
        self._store_citations(actor, kb_id, build_id, snapshot, generated)
        stored = {r['citation_id']: r for r in self.repository.citations(actor, kb_id, build_id)}
        if len(units) != build['unit_count'] or expected != stored:
            raise PlatformError('STALE_BUILD', 'Regenerated units differ from committed evidence', 409)

        def authorize():
            with self._tx(actor, kb_id) as conn:
                if self._lock_authority(conn, actor) != revision:
                    raise PlatformError('STALE_BUILD', 'Authority changed during build', 409)
                current = self._auth(conn, actor, kb_id, 'kb.manage')
                if current['revision'] != build['kb_revision'] or current['manifest_id'] != build['expected_manifest_id']:
                    raise PlatformError('STALE_BUILD', 'Knowledge base changed during build', 409)
            return True

        if config['embedding']:
            if self.embedding_provider is None or self.embedding_provider.manifest != config['embedding']:
                raise PlatformError('STALE_BUILD', 'Embedding provider differs from frozen build', 409)
            eligible = [u for u in units if u['source_id'] in config['vector_source_ids'] and u['kind'] != 'Page']
            context = embedding_audit(actor, kb_id, build_id, revision, rows)
            jobs = self.embedding_jobs
            jobs.reconcile(actor.tenant_id, kb_id, build_id, self.embedding_provider, authorize)
            try:
                encoded = jobs.run(actor.tenant_id, kb_id, build_id, self.embedding_provider, eligible, authorize, context)
            except EmbeddingJobError as exc:
                message = f'Build {build_id}: {exc.code}'
                if exc.retry_at:
                    message += f'; retry_at={exc.retry_at.isoformat()}'
                raise PlatformError(exc.code, message, 503) from exc
            vectors = {u['unit_id']: u['vector'] for u in encoded}
            units = [{**u, **({'vector': vectors[u['unit_id']]} if u['unit_id'] in vectors else {})} for u in units]

        authorize()
        try:
            try:
                existing = self.backend.read_units(build['index_name'])
            except SearchUnavailable as exc:
                if str(exc) != 'Search generation not found' and not isinstance(exc, SearchGenerationIncomplete):
                    raise
                self.backend.stage(build['index_name'], units,
                                   vector_dimension=self.embedding_provider.dimension if config['embedding'] else None)
            else:
                if existing != sorted(units, key=content_order):
                    raise PlatformError('BUILD_CONTENT_MISMATCH', 'Stored generation differs from frozen inputs', 409)
        except SearchUnavailable as exc:
            raise PlatformError('SEARCH_UNAVAILABLE', str(exc), 503) from exc
        with self._tx(actor, kb_id) as conn:
            if self._lock_authority(conn, actor) != revision:
                raise PlatformError('STALE_BUILD', 'Authority changed before validation', 409)
            current = self._auth(conn, actor, kb_id, 'kb.manage')
            if current['revision'] != build['kb_revision'] or current['manifest_id'] != build['expected_manifest_id']:
                raise PlatformError('STALE_BUILD', 'Knowledge base changed before validation', 409)
            conn.execute(KnowledgeQuery.BUILD_VALIDATE_STAGED,
                         (actor.tenant_id, kb_id, build_id))
        return {'build_id': build_id, 'kb_id': kb_id, 'status': 'VALIDATED', 'unit_count': len(units),
                'channels': config['channels'], 'expected_manifest_id': build['expected_manifest_id']}

    def _embedding_allowed(self, row):
        return (self.embedding_provider is not None and row['policy'].get('embedding', False) and
                self.embedding_provider.provider_id in row['policy'].get('allowed_providers', []))

    def _units(self, actor, kb_id, build_id, rows):
        from .delivery_units import indexed_elements

        units, citations = [], []
        for row in rows:
            doc = row['document']
            anchors = {a['anchor_id']: a for a in doc['anchors']}
            evidence = {e['evidence_id']: e for e in doc['evidence_spans']}
            retrieval_elements = indexed_elements(doc, include_pages=True)
            ids = {e['element_id']: str(uuid5(NAMESPACE_URL, f'{build_id}/{row["binding_id"]}/{e["element_id"]}'))
                   for e in retrieval_elements}
            for element in retrieval_elements:
                text = element.get('raw_text', '')
                if not text.strip():
                    continue
                evidence_ids = element.get('evidence_ids', [])
                refs = [a for eid in evidence_ids for a in evidence.get(eid, {}).get('anchor_ids', [])]
                unit_anchors = [anchors[a] for a in dict.fromkeys(refs) if a in anchors]
                if not evidence_ids or not unit_anchors:
                    raise PlatformError('INVALID_EVIDENCE', 'Indexed unit has no resolvable original evidence', 422)
                unit_id = ids[element['element_id']]
                unit = {'tenant_id': actor.tenant_id, 'kb_id': kb_id, 'build_id': build_id,
                        'binding_id': row['binding_id'], 'source_id': row['source_id'],
                        'source_version_id': row['source_version_id'], 'parse_snapshot_id': row['parse_snapshot_id'],
                        'unit_id': unit_id, 'citation_id': unit_id, 'text': text,
                        'kind': element['kind'], 'ordinal': element['ordinal'],
                        'parent_id': ids.get(element.get('parent_id')), 'heading_text': ' '.join(element.get('heading_path', [])),
                        'evidence_ids': evidence_ids, 'anchors': unit_anchors}
                units.append(unit)
                citations.append((actor.tenant_id, kb_id, unit_id, build_id, row['binding_id'], row['source_id'],
                                  row['source_version_id'], row['parse_snapshot_id'], element['element_id'], text,
                                  evidence_ids, unit_anchors, element.get('table')))
        return units, citations

    def publish(self, actor, kb_id, build_id):
        with self._tx(actor, kb_id) as conn:
            authority_revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage')
            build = conn.execute(KnowledgeQuery.BUILD_LOCK,
                                 (actor.tenant_id, kb_id, build_id)).fetchone()
            if not build:
                _deny()
            if build['status'] == 'PUBLISHED' and kb['manifest_id'] == build_id:
                return {'kb_id': kb_id, 'manifest_id': build_id, 'status': 'PUBLISHED', 'idempotent': True}
            if build['status'] != 'VALIDATED':
                raise PlatformError('BUILD_NOT_VALIDATED', 'Build is not ready to publish', 409)
            if (build['expected_manifest_id'] != kb['manifest_id'] or
                build['authority_revision'] != authority_revision or build['kb_revision'] != kb['revision']):
                raise PlatformError('STALE_BUILD', 'Build inputs or authorization changed', 409)
            conn.execute(KnowledgeQuery.MANIFEST_UPDATE,
                         (build_id, actor.tenant_id, kb_id))
            conn.execute(KnowledgeQuery.BUILD_PUBLISH,
                         (actor.tenant_id, kb_id, build_id))
        return {'kb_id': kb_id, 'manifest_id': build_id, 'status': 'PUBLISHED', 'idempotent': False}

    def search(self, actor, kb_id, query, channel='bm25', top_k=5, context_budget=4000, purpose='human_search'):
        if channel not in {'bm25', 'structure', 'table', 'dense', 'hybrid'}:
            raise PlatformError('CHANNEL_NOT_ADMITTED', 'Channel has no approved configured provider', 409)
        if not query.strip() or len(query) > 2000 or not 1 <= top_k <= 50 or not 1 <= context_budget <= 50000:
            raise PlatformError('INVALID_QUERY', 'Invalid query or retrieval budget')
        snapshot = self._source_snapshot(actor, kb_id, published=True)
        with self._tx(actor, kb_id) as conn:
            self._check_source_snapshot(conn, actor, kb_id, snapshot)
            kb = self._auth(conn, actor, kb_id)
            revision = self._revision(conn, actor)
            allowed = self._allowed(snapshot['rows'], actor, purpose)
            if not kb['manifest_id']:
                raise PlatformError('NO_PUBLISHED_MANIFEST', 'Knowledge base has no published index', 409)
            build = conn.execute(KnowledgeQuery.BUILD_PUBLISHED, (actor.tenant_id, kb_id, kb['manifest_id'])).fetchone()
            if not build:
                raise PlatformError('INVALID_MANIFEST', 'Published index unavailable', 503)
            config = build['inputs']
            if channel not in config['channels']:
                raise PlatformError('CHANNEL_NOT_ADMITTED', 'Channel was not published in this manifest', 409)
            model_query = channel in {'dense', 'hybrid'}
            if model_query and (self.embedding_provider is None or
                                self.embedding_provider.manifest != config.get('embedding')):
                raise PlatformError('MODEL_VERSION_MISMATCH', 'Published embedding model is not configured', 409)
            vector_allowed = [row for row in allowed if self._embedding_allowed(row) and
                              row['source_id'] in config.get('vector_source_ids', [])] if model_query else []
        candidate_budget = max(20, top_k)
        try:
            dense_hits = []
            if model_query and vector_allowed:
                provider = self.embedding_provider
                if getattr(provider, 'requires_authorization', False):
                    def authorize():
                        with self._tx(actor, kb_id) as conn:
                            if self._lock_authority(conn, actor) != revision:
                                raise PlatformError('SCOPE_CHANGED', 'Authorization changed before model processing', 409)
                            current = self._auth(conn, actor, kb_id)
                            if current['manifest_id'] != kb['manifest_id']:
                                raise PlatformError('SCOPE_CHANGED', 'Published index changed before model processing', 409)
                        return True

                    vector = provider.query(query, request_id='knowledge-query-' + uuid4().hex,
                        audit_context=embedding_audit(actor, kb_id, kb['manifest_id'], revision,
                                                     vector_allowed, purpose=purpose),
                        authorize=authorize)
                else:
                    with self._tx(actor, kb_id) as conn:
                        if self._lock_authority(conn, actor) != revision:
                            raise PlatformError('SCOPE_CHANGED', 'Authorization changed before model processing', 409)
                        self._auth(conn, actor, kb_id)
                        vector = provider.query(query)
                dense_hits = self.backend.dense(build['index_name'], actor.tenant_id, kb_id, vector_allowed,
                                                vector, candidate_budget // 2 if channel == 'hybrid' else candidate_budget)
                vector_keys = {(row['binding_id'], row['source_id'], row['source_version_id'], row['parse_snapshot_id'])
                               for row in vector_allowed}
                if any((hit.get('binding_id'), hit.get('source_id'), hit.get('source_version_id'),
                        hit.get('parse_snapshot_id')) not in vector_keys for hit in dense_hits):
                    raise PlatformError('INDEX_SCOPE_VIOLATION', 'Vector result violates processing policy', 503)
            if channel == 'dense':
                candidates = dense_hits
            else:
                candidates = self.backend.search(build['index_name'], actor.tenant_id, kb_id, allowed, query,
                                                 (candidate_budget + 1) // 2 if channel == 'hybrid' else candidate_budget,
                                                 kind='Table' if channel == 'table' else None)
            if channel == 'hybrid':
                fused, scores = {}, {}
                for ranking in (candidates, dense_hits):
                    for rank, hit in enumerate(ranking, 1):
                        key = hit['unit_id']
                        fused[key] = hit
                        scores[key] = scores.get(key, 0) + 1 / (60 + rank)
                candidates = [{**fused[key], 'score': scores[key]} for key in
                              sorted(fused, key=lambda key: (-scores[key], content_order(fused[key])))]
            if channel == 'structure':
                expanded = []
                for hit in candidates:
                    expanded.extend(self.backend.expand(build['index_name'], actor.tenant_id, kb_id, hit))
                candidates = list({hit['unit_id']: hit for hit in expanded}.values())
        except SearchUnavailable as exc:
            raise PlatformError('SEARCH_UNAVAILABLE', str(exc), 503) from exc
        except EmbeddingError as exc:
            raise PlatformError('EMBEDDING_FAILED', str(exc), 422) from exc
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            current = self._auth(conn, actor, kb_id)
            if revision != self._revision(conn, actor) or current['manifest_id'] != kb['manifest_id']:
                raise PlatformError('SCOPE_CHANGED', 'Authorization or published version changed; retry', 409)
            allowed_keys = {(r['binding_id'], r['source_id'], r['source_version_id'], r['parse_snapshot_id'])
                            for r in allowed}
            for hit in candidates:
                if (hit.get('tenant_id') != actor.tenant_id or hit.get('kb_id') != kb_id or
                    hit.get('build_id') != build['build_id'] or
                    (hit.get('binding_id'), hit.get('source_id'), hit.get('source_version_id'), hit.get('parse_snapshot_id'))
                    not in allowed_keys):
                    raise PlatformError('INDEX_SCOPE_VIOLATION', 'Index scope validation failed', 503)
                record = conn.execute(KnowledgeQuery.CITATION_GET, (actor.tenant_id, kb_id, hit.get('citation_id'),
                                                         build['build_id'])).fetchone()
                keys = ('source_id', 'binding_id', 'source_version_id', 'parse_snapshot_id',
                        'text', 'evidence_ids', 'anchors')
                if (not record or hit.get('unit_id') != record['citation_id'] or
                        any(hit.get(key) != record[key] for key in keys)):
                    raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Index evidence differs from authority', 503)
                score = hit.get('score')
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Index score is invalid', 503)
                hit.clear()
                hit.update({key: record[key] for key in (*keys, 'tenant_id', 'kb_id', 'build_id', 'citation_id')})
                hit.update(unit_id=record['citation_id'], score=score)
            hits, used = [], 0
            for hit in candidates:
                cost = len(hit['text']) + (2 if hits else 0)
                if used + cost > context_budget:
                    continue
                if len(hits) >= top_k:
                    break
                hits.append(hit)
                used += cost
        return {'kb_id': kb_id, 'manifest_id': kb['manifest_id'], 'channel': channel, 'hits': hits,
                'context': '\n\n'.join(hit['text'] for hit in hits),
                'trace': {'candidate_source_ids': list(dict.fromkeys(h['source_id'] for h in candidates)),
                          'authority_revision': revision, 'context_budget': context_budget,
                          'context_budget_unit': 'unicode_code_point', 'context_used': used,
                          'omitted_for_budget': len(candidates) - len(hits),
                          'candidate_budget': candidate_budget, 'model_query_executed': bool(model_query and vector_allowed),
                          'lexical_query_protocol': QUERY_PROTOCOL if channel != 'dense' else None,
                          'lexical_prefetch_budget': max(30, (candidate_budget + 1) // 2 if channel == 'hybrid' else candidate_budget) if channel != 'dense' else 0,
                          'model_admission': config.get('model_admission', 'NOT_ENABLED'),
                          'tie_break': list(TIE_BREAK_FIELDS),
                          'fusion': {'method': 'rrf', 'k': 60} if channel == 'hybrid' else None}}

    def authorize_ingest_result(self, actor, kb_id, source_id, source_version_id, parse_snapshot_id):
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            self._source(conn, actor, source_id, owner=True)
            row = conn.execute(KnowledgeQuery.BINDING_ACCESS,
                               (actor.tenant_id, kb_id, source_id)).fetchone()
            if (not row or row['source_version_id'] != source_version_id
                    or row['parse_snapshot_id'] != parse_snapshot_id):
                _deny()

    def authorize_original(self, actor, kb_id, citation_id, purpose='preview', *, processing_policy=None):
        if purpose not in {'preview', 'download', 'llm_context'}:
            raise PlatformError('INVALID_PURPOSE', 'Unknown citation purpose')
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id)
            if processing_policy is None:
                row = conn.execute(KnowledgeQuery.CITATION_RESOLVE,
                                   (actor.tenant_id, kb_id, citation_id)).fetchone()
            else:
                row = conn.execute(KnowledgeQuery.CITATION_ACCESS,
                                   (actor.tenant_id, kb_id, citation_id)).fetchone()
            if row and processing_policy is not None:
                row = {**row, 'document': {'processing_policy': processing_policy}}
            if not row or not self._allowed([row], actor, purpose):
                _deny()
            result = {key: row[key] for key in ('citation_id', 'kb_id', 'source_id', 'source_version_id', 'filename',
                    'original_path', 'original_sha256', 'text', 'anchors', 'media_type', 'policy', 'table_data',
                    'build_id', 'parse_snapshot_id', 'evidence_ids')}
            result['processing_policy'] = dict(row.get('document', {}).get('processing_policy', {}))
            return result

    def resolve_citation(self, actor, kb_id, citation_id, purpose='preview'):
        target = self.authorize_original(actor, kb_id, citation_id, purpose)
        if purpose == 'download':
            try:
                self.store.read(target['original_path'], actor.tenant_id, target['original_sha256'])
            except (OSError, ValueError) as exc:
                raise PlatformError('SOURCE_INTEGRITY', 'Original backup unavailable or corrupt', 503) from exc
        return {key: value for key, value in target.items() if key != 'processing_policy'}

    def read_original(self, actor, kb_id, citation_id, purpose='download'):
        target = self.authorize_original(actor, kb_id, citation_id, purpose)
        try:
            data = self.store.read(target['original_path'], actor.tenant_id, target['original_sha256'])
        except (OSError, ValueError) as error:
            raise PlatformError('SOURCE_INTEGRITY', 'Original backup unavailable or corrupt', 503) from error
        current = self.authorize_original(actor, kb_id, citation_id, purpose,
                                          processing_policy=target['processing_policy'])
        if any(current[key] != target[key] for key in (
                'source_id', 'source_version_id', 'parse_snapshot_id', 'original_sha256', 'build_id')):
            raise PlatformError('SOURCE_INTEGRITY', 'Original identity changed during read', 503)
        return data

    def table_query(self, actor, kb_id, citation_id, operation='rows', column=None, filters=None):
        citation = self.resolve_citation(actor, kb_id, citation_id)
        table = citation.get('table_data')
        if not table or operation not in {'rows', 'count', 'sum', 'min', 'max'}:
            raise PlatformError('INVALID_TABLE_QUERY', 'Unsupported table operation')
        headers, rows = table['headers'], table['rows']
        filters = filters or []
        if not isinstance(filters, list) or len(filters) > 10:
            raise PlatformError('INVALID_TABLE_QUERY', 'Invalid table filters')

        def number(value):
            if isinstance(value, bool) or value is None:
                raise PlatformError('NON_NUMERIC_CELL', 'Numeric operation requires verified values', 422)
            try:
                numeric = Decimal(str(value))
                if not numeric.is_finite():
                    raise InvalidOperation()
                return numeric
            except InvalidOperation as exc:
                raise PlatformError('NON_NUMERIC_CELL', 'Numeric operation requires verified values', 422) from exc

        for item in filters:
            if not isinstance(item, dict) or item.get('column') not in headers or item.get('op') not in {'eq','lt','lte','gt','gte'}:
                raise PlatformError('INVALID_TABLE_QUERY', 'Unsupported table filter')
            idx = headers.index(item['column'])
            op, value = item['op'], item.get('value')
            selected = []
            for row in rows:
                cell = row[idx] if idx < len(row) else None
                if op == 'eq':
                    match = cell == value
                else:
                    left, right = number(cell), number(value)
                    match = {'lt': left < right, 'lte': left <= right, 'gt': left > right, 'gte': left >= right}[op]
                if match:
                    selected.append(row)
            rows = selected
        if operation == 'rows':
            result = rows[:1000]
        elif operation == 'count':
            result = len(rows)
        else:
            if column not in headers:
                raise PlatformError('INVALID_TABLE_QUERY', 'Unknown table column')
            idx = headers.index(column)
            values = [number(row[idx] if idx < len(row) else None) for row in rows]
            if not values:
                raise PlatformError('NO_NUMERIC_VALUES', 'No numeric values in selected range', 422)
            result = str(sum(values) if operation == 'sum' else min(values) if operation == 'min' else max(values))
        self.resolve_citation(actor, kb_id, citation_id)
        return {'operation': operation, 'result': result, 'headers': headers, 'matched_rows': len(rows),
                'citation_id': citation_id, 'source_version_id': citation['source_version_id'],
                'anchors': citation['anchors']}

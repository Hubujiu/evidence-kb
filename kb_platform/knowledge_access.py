from hashlib import sha256
import json
from threading import Lock

from .core import Platform
from .knowledge_queries import KnowledgeQuery
from .session_identity import Actor, PlatformError


class KnowledgeAccess:
    def __init__(self, repository):
        self._platform = Platform(repository=repository, search=None, store=None, parser=None,
                                  embedding_jobs=None)
        self._verified = set()
        self._verified_lock = Lock()

    def authorize(self, request):
        actor, scope = Actor(**request['actor']), request['scope_id']
        with self._platform._tx(actor, scope) as connection:
            revision = self._platform._lock_authority(connection, actor)
            kb = self._platform._auth(connection, actor, scope)
            return {'scope_id': scope, 'manifest_id': kb['manifest_id'], 'authority_revision': revision}

    def _embedding_build(self, connection, actor, scope, audit, task):
        revision = self._platform._lock_authority(connection, actor)
        if str(revision) != audit['acl_revision']:
            raise PlatformError('STALE_BUILD', 'Authorization changed before embedding', 409)
        kb = self._platform._auth(connection, actor, scope)
        build = connection.execute(KnowledgeQuery.BUILD_GET,
            (actor.tenant_id, scope, audit['build_id'])).fetchone()
        if build is None:
            raise PlatformError('STALE_BUILD', 'Embedding build is unavailable', 409)
        if task == 'document':
            self._platform._auth(connection, actor, scope, 'kb.manage')
            if (build['status'] != 'STAGING' or build['authority_revision'] != revision
                    or build['kb_revision'] != kb['revision']
                    or build['expected_manifest_id'] != kb['manifest_id']):
                raise PlatformError('STALE_BUILD', 'Embedding build inputs changed', 409)
        elif build['status'] != 'PUBLISHED' or kb['manifest_id'] != audit['build_id']:
            raise PlatformError('STALE_BUILD', 'Embedding query does not target the current manifest', 409)
        return kb, build

    def validate_embedding(self, request):
        audit, task, provider = request['audit_context'], request['task'], request['provider_id']
        identifiers = ('tenant_id', 'kb_id', 'build_id', 'principal', 'acl_revision')
        sources = audit.get('sources')
        purpose = audit.get('purpose', 'llm_context')
        if (task not in {'document', 'query'} or not isinstance(provider, str) or not provider
                or purpose not in {'human_search', 'llm_context'}
                or any(not isinstance(audit.get(key), str) or not audit[key] for key in identifiers)
                or not isinstance(sources, list) or not sources):
            raise PlatformError('INVALID_EMBEDDING_AUTHORIZATION', 'Embedding authority identity is incomplete', 422)
        fields = ('source_id', 'source_version_id', 'parse_snapshot_id', 'source_text_sha256')
        if (any(not isinstance(source, dict) or any(not isinstance(source.get(key), str)
                or not source[key] for key in fields) for source in sources)
                or len({source['source_id'] for source in sources}) != len(sources)):
            raise PlatformError('INVALID_EMBEDDING_AUTHORIZATION', 'Embedding source identity is invalid', 422)
        actor, scope = Actor(audit['tenant_id'], audit['principal']), audit['kb_id']
        with self._platform._tx(actor, scope) as connection:
            kb, build = self._embedding_build(connection, actor, scope, audit, task)
            proof = sha256(json.dumps({'actor': [actor.tenant_id, actor.principal_id], 'scope': scope,
                'build': build, 'kb_revision': kb['revision'], 'manifest': kb['manifest_id'],
                'task': task, 'provider': provider, 'audit': audit},
                sort_keys=True, ensure_ascii=True).encode()).hexdigest()
            with self._verified_lock:
                if proof in self._verified:
                    return {'authorized': True}
        snapshot = self._platform._source_snapshot(actor, scope, processing=True,
            capability='kb.manage' if task == 'document' else 'kb.read')
        rows = snapshot['rows']
        allowed = self._platform._allowed(rows, actor, 'human_search' if task == 'document' else purpose)
        current = {row['source_id']: row for row in allowed}
        processing = set(build['inputs'].get('vector_source_ids', [])) if task == 'document' else {
            source['source_id'] for source in sources}
        if task == 'document' and (not processing
                or (build['inputs'].get('embedding') or {}).get('provider') != provider):
            raise PlatformError('PROCESSING_NOT_ALLOWED', 'Build does not admit this embedding provider', 403)
        if task == 'document' and processing - {source['source_id'] for source in sources}:
            raise PlatformError('INVALID_EMBEDDING_AUTHORIZATION', 'Embedding audit omits build sources', 422)
        for source in sources:
            row = current.get(source['source_id'])
            if row is None:
                raise PlatformError('ACCESS_DENIED', 'Embedding source is not currently authorized', 403)
            actual = {'source_id': row['source_id'], 'source_version_id': row['source_version_id'],
                'parse_snapshot_id': row['parse_snapshot_id'], 'source_text_sha256': sha256(
                    '\n'.join(element['raw_text'] for element in row['document']['elements']).encode()).hexdigest()}
            if any(source[key] != actual[key] for key in fields):
                raise PlatformError('STALE_BUILD', 'Embedding source content changed', 409)
            if (row['source_id'] in processing and (not row['policy'].get('embedding', False)
                    or provider not in row['policy'].get('allowed_providers', []))):
                raise PlatformError('PROCESSING_NOT_ALLOWED', 'Source does not admit this embedding provider', 403)
        with self._platform._tx(actor, scope) as connection:
            self._platform._check_source_snapshot(connection, actor, scope, snapshot)
            current_kb, current_build = self._embedding_build(connection, actor, scope, audit, task)
            if current_build != build or current_kb != kb:
                raise PlatformError('STALE_BUILD', 'Embedding inputs changed during validation', 409)
        with self._verified_lock:
            if len(self._verified) >= 128:
                self._verified.clear()
            self._verified.add(proof)
        return {'authorized': True}

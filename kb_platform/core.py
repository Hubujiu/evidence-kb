from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4, uuid5, NAMESPACE_URL

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .search import OpenSearch, SearchUnavailable, TIE_BREAK_FIELDS, content_order, QUERY_PROTOCOL
from .storage import OriginalStore
from .embeddings import EmbeddingError


CAPABILITIES = {'kb.read', 'kb.write', 'kb.manage', 'kb.manage_members', 'kb.manage_managers',
                'kb.delete', 'kb.audit'}
DEFAULT_POLICY = {'native_parse': True, 'ocr': False, 'embedding': False, 'rerank': False,
                  'llm_context': False, 'compile': False, 'preview': True, 'download': True,
                  'allowed_providers': [], 'include_hidden': False, 'include_notes': False}


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    principal_id: str

    def __post_init__(self):
        if not self.tenant_id or not self.principal_id:
            raise ValueError('Identity requires tenant and principal')


class PlatformError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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


def _parse(data: bytes, filename: str, policy: dict) -> dict:
    if (isinstance(data, bytes) and isinstance(filename, str) and len(data) <= 64 * 1024
            and Path(filename).suffix.lower() in {'.md', '.txt'}):
        try:
            from .documents import parse_document

            return parse_document(data, filename, policy)
        except Exception as exc:
            raise PlatformError('PARSE_FAILED', 'Document parsing failed', 422) from exc
    code = ('import base64,json,sys; from kb_platform.documents import parse_document; '
            'x=json.load(sys.stdin); '
            'print(json.dumps(parse_document(base64.b64decode(x["data"]),x["filename"],x["policy"])))')
    payload = json.dumps({'data': base64.b64encode(data).decode(), 'filename': filename, 'policy': policy})
    try:
        timeout = max(1, min(3600, float(os.environ.get('EVIDENCE_KB_PARSE_TIMEOUT_SECONDS', '45'))))
        result = subprocess.run([sys.executable, '-c', code], input=payload, capture_output=True,
                                encoding='utf-8', timeout=timeout, cwd=Path(__file__).resolve().parent.parent)
    except subprocess.TimeoutExpired as exc:
        raise PlatformError('PARSE_TIMEOUT', 'Document parsing exceeded its configured timeout', 422) from exc
    if result.returncode:
        raise PlatformError('PARSE_FAILED', 'Document parsing failed', 422)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise PlatformError('PARSE_FAILED', 'Document parser returned invalid output', 422) from exc


class Platform:
    def __init__(self, dsn: str, search_url: str, storage_root, embedding_provider=None, ocr_client=None):
        self.dsn = dsn
        self.backend = OpenSearch(search_url)
        self.store = OriginalStore(storage_root)
        self.embedding_provider = embedding_provider
        self.ocr_client = ocr_client

    @contextmanager
    def _tx(self, actor: Actor, kb_id: str | None = None):
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as conn:
                conn.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.kb_id', %s, true)",
                             (actor.tenant_id, kb_id or ''))
                yield conn
        except psycopg.OperationalError as exc:
            raise PlatformError('AUTHORITY_UNAVAILABLE', 'Authorization service unavailable', 503) from exc

    def _auth(self, conn, actor: Actor, kb_id: str, capability='kb.read') -> dict:
        if not kb_id:
            _deny()
        row = conn.execute('SELECT k.*, m.capabilities FROM knowledge_bases k JOIN members m '
                           'USING (tenant_id,kb_id) WHERE k.tenant_id=%s AND k.kb_id=%s AND m.principal_id=%s',
                           (actor.tenant_id, kb_id, actor.principal_id)).fetchone()
        if not row or capability not in row['capabilities']:
            _deny()
        return row

    def _lock_authority(self, conn, actor: Actor) -> int:
        row = conn.execute('SELECT revision FROM tenant_state WHERE tenant_id=%s FOR UPDATE',
                           (actor.tenant_id,)).fetchone()
        if not row:
            _deny()
        return row['revision']

    def _revision(self, conn, actor: Actor) -> int:
        row = conn.execute('SELECT revision FROM tenant_state WHERE tenant_id=%s',
                           (actor.tenant_id,)).fetchone()
        if not row:
            _deny()
        return row['revision']

    def _bump(self, conn, actor: Actor, kb_id: str | None = None):
        conn.execute('UPDATE tenant_state SET revision=revision+1 WHERE tenant_id=%s', (actor.tenant_id,))
        if kb_id:
            conn.execute('UPDATE knowledge_bases SET revision=revision+1 WHERE tenant_id=%s AND kb_id=%s',
                         (actor.tenant_id, kb_id))

    def _source(self, conn, actor: Actor, source_id: str, owner=False) -> dict:
        row = conn.execute('SELECT * FROM sources WHERE tenant_id=%s AND source_id=%s',
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
            conn.execute('INSERT INTO tenant_state (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING',
                         (actor.tenant_id,))
            self._lock_authority(conn, actor)
            conn.execute('INSERT INTO knowledge_bases (tenant_id,kb_id,name,owner_id) VALUES (%s,%s,%s,%s)',
                         (actor.tenant_id, kb_id, name, actor.principal_id))
            conn.execute('INSERT INTO members VALUES (%s,%s,%s,%s)',
                         (actor.tenant_id, kb_id, actor.principal_id, Jsonb(sorted(CAPABILITIES))))
            self._bump(conn, actor)
        return {'kb_id': kb_id, 'name': name, 'tenant_id': actor.tenant_id, 'manifest_id': None}

    def list_kbs(self, actor: Actor) -> list[dict]:
        with self._tx(actor) as conn:
            return conn.execute('SELECT k.kb_id,k.name,k.manifest_id,m.capabilities FROM knowledge_bases k '
                                'JOIN members m USING (tenant_id,kb_id) '
                                'WHERE k.tenant_id=%s AND m.principal_id=%s AND m.capabilities<>\'[]\'::jsonb '
                                'ORDER BY k.name,k.kb_id',
                                (actor.tenant_id, actor.principal_id)).fetchall()

    def set_member(self, actor: Actor, kb_id: str, principal_id: str, capabilities: list[str]) -> dict:
        if not principal_id or set(capabilities) - CAPABILITIES:
            raise PlatformError('INVALID_CAPABILITIES', 'Invalid member capabilities')
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage_members')
            previous = conn.execute('SELECT capabilities FROM members WHERE tenant_id=%s AND kb_id=%s '
                                    'AND principal_id=%s', (actor.tenant_id, kb_id, principal_id)).fetchone()
            management = {'kb.manage', 'kb.manage_members', 'kb.manage_managers', 'kb.delete'}
            affected = set(capabilities) | set(previous['capabilities'] if previous else [])
            if (not set(capabilities).issubset(kb['capabilities']) or
                (affected & management and 'kb.manage_managers' not in kb['capabilities'])):
                _deny()
            if principal_id == kb['owner_id'] and set(capabilities) != CAPABILITIES:
                raise PlatformError('OWNER_REQUIRED', 'Owner capabilities cannot be removed', 409)
            conn.execute('INSERT INTO members VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,kb_id,principal_id) '
                         'DO UPDATE SET capabilities=EXCLUDED.capabilities',
                         (actor.tenant_id, kb_id, principal_id, Jsonb(sorted(set(capabilities)))))
            self._bump(conn, actor, kb_id)
        return {'principal_id': principal_id, 'capabilities': sorted(set(capabilities))}

    def ingest(self, actor: Actor, kb_id: str, filename: str, data: bytes,
               readers: list[str] | None = None, policy: dict | None = None,
               source_id: str | None = None) -> dict:
        from .documents import max_upload_bytes

        limit = max_upload_bytes()
        if not data or len(data) > limit:
            raise PlatformError('FILE_SIZE', f'File must contain 1 to {limit} bytes', 422)
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        if not filename or len(filename) > 255:
            raise PlatformError('INVALID_FILENAME', 'Invalid filename', 422)
        with self._tx(actor, kb_id) as conn:
            self._auth(conn, actor, kb_id, 'kb.write')
            existing = self._source(conn, actor, source_id, owner=True) if source_id else None
            parse_policy = _policy(policy if policy is not None else existing['policy'] if existing else None)
            parse_authorization = {'principal': f'{actor.tenant_id}:{actor.principal_id}',
                                   'acl_revision': str(self._revision(conn, actor))}
        path, digest = self.store.put(actor.tenant_id, data)
        document = _parse(data, filename, {**parse_policy, '_parse_authorization': parse_authorization})
        if (self.ocr_client is not None and parse_policy.get('ocr') and
                self.ocr_client.provider_id in parse_policy['allowed_providers'] and
                document['status'] in {'PARTIAL', 'BLOCKED'}):
            from .ocr import augment_ocr

            document = augment_ocr(data, filename, document, self.ocr_client)
        document['processing_policy'] = parse_policy
        if document.get('original_sha256') != digest or document.get('source_version_id') != digest:
            raise PlatformError('SOURCE_INTEGRITY', 'Parser source identity mismatch', 422)
        source_id = source_id or str(uuid4())
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            if existing:
                current = self._source(conn, actor, source_id, owner=True)
                if current['revision'] != existing['revision']:
                    raise PlatformError('STALE_PARSE', 'Source policy changed during parsing', 409)
                effective_readers = readers if readers is not None else current['readers']
                conn.execute('UPDATE sources SET readers=%s,policy=%s,revision=revision+1 '
                             'WHERE tenant_id=%s AND source_id=%s',
                             (Jsonb(effective_readers), Jsonb(parse_policy), actor.tenant_id, source_id))
            else:
                conn.execute('INSERT INTO sources (tenant_id,source_id,owner_id,readers,policy) VALUES (%s,%s,%s,%s,%s)',
                             (actor.tenant_id, source_id, actor.principal_id, Jsonb(readers or []), Jsonb(parse_policy)))
            conn.execute('INSERT INTO source_versions VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (actor.tenant_id, source_id, digest, filename, path, digest, document['media_type']))
            conn.execute('INSERT INTO parse_snapshots VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (actor.tenant_id, source_id, digest, document['parse_snapshot_id'], Jsonb(document)))
            binding = self._bind(conn, actor, kb_id, source_id, digest, document['parse_snapshot_id'])
            self._bump(conn, actor, kb_id)
        return {**binding, 'document': document}

    def _bind(self, conn, actor, kb_id, source_id, source_version_id, parse_id):
        row = conn.execute('INSERT INTO bindings (tenant_id,kb_id,binding_id,source_id,source_version_id,'
                           'parse_snapshot_id,status) VALUES (%s,%s,%s,%s,%s,%s,\'ACTIVE\') '
                           'ON CONFLICT (tenant_id,kb_id,source_id) DO UPDATE SET '
                           'source_version_id=EXCLUDED.source_version_id,parse_snapshot_id=EXCLUDED.parse_snapshot_id,'
                           'status=\'ACTIVE\',revision=bindings.revision+1 RETURNING binding_id,source_id,source_version_id',
                           (actor.tenant_id, kb_id, str(uuid4()), source_id, source_version_id, parse_id)).fetchone()
        return row

    def bind_source(self, actor, kb_id, source_id, source_version_id):
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            self._source(conn, actor, source_id, owner=True)
            snapshot = conn.execute('SELECT parse_snapshot_id FROM parse_snapshots WHERE tenant_id=%s '
                                    'AND source_id=%s AND source_version_id=%s ORDER BY parse_snapshot_id LIMIT 1',
                                    (actor.tenant_id, source_id, source_version_id)).fetchone()
            if not snapshot:
                _deny()
            result = self._bind(conn, actor, kb_id, source_id, source_version_id, snapshot['parse_snapshot_id'])
            self._bump(conn, actor, kb_id)
            return result

    def remove_binding(self, actor, kb_id, binding_id):
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id, 'kb.write')
            row = conn.execute('UPDATE bindings SET status=\'REMOVED\',revision=revision+1 '
                               'WHERE tenant_id=%s AND kb_id=%s AND binding_id=%s RETURNING binding_id,status',
                               (actor.tenant_id, kb_id, binding_id)).fetchone()
            if not row:
                _deny()
            self._bump(conn, actor, kb_id)
            return row

    def set_source_readers(self, actor, source_id, readers):
        if not isinstance(readers, list) or any(not isinstance(p, str) or not p for p in readers):
            raise PlatformError('INVALID_READERS', 'Readers must be principal identifiers')
        with self._tx(actor) as conn:
            self._lock_authority(conn, actor)
            self._source(conn, actor, source_id, owner=True)
            conn.execute('UPDATE sources SET readers=%s,revision=revision+1 WHERE tenant_id=%s AND source_id=%s',
                         (Jsonb(sorted(set(readers))), actor.tenant_id, source_id))
            self._bump(conn, actor)
        return {'source_id': source_id, 'readers': sorted(set(readers))}

    def set_source_policy(self, actor, source_id, policy):
        policy = _policy(policy)
        with self._tx(actor) as conn:
            self._lock_authority(conn, actor)
            self._source(conn, actor, source_id, owner=True)
            conn.execute('UPDATE sources SET policy=%s,revision=revision+1 WHERE tenant_id=%s AND source_id=%s',
                         (Jsonb(policy), actor.tenant_id, source_id))
            self._bump(conn, actor)
        return {'source_id': source_id, 'policy': policy}

    def _bindings(self, conn, actor, kb_id):
        return conn.execute('SELECT b.*,s.owner_id,s.readers,s.policy,s.revision AS source_revision,p.document '
                            'FROM bindings b JOIN sources s USING (tenant_id,source_id) JOIN parse_snapshots p '
                            'USING (tenant_id,source_id,source_version_id,parse_snapshot_id) '
                            'WHERE b.tenant_id=%s AND b.kb_id=%s AND b.status=\'ACTIVE\' ORDER BY b.binding_id',
                            (actor.tenant_id, kb_id)).fetchall()

    def _allowed(self, rows, actor, purpose):
        if purpose not in {'human_search', 'llm_context', 'preview', 'download'}:
            raise PlatformError('INVALID_PURPOSE', 'Unknown access purpose')
        return [row for row in rows if
                (row['owner_id'] == actor.principal_id or actor.principal_id in row['readers']) and
                not any(row.get('document', {}).get('processing_policy', {}).get(flag, False)
                        and not row['policy'].get(flag, False) for flag in ('include_hidden', 'include_notes', 'ocr')) and
                (purpose == 'human_search' or row['policy'].get(purpose, False))]

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
        with self._tx(actor, kb_id) as conn:
            authority_revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage')
            self._auth(conn, actor, kb_id, 'kb.read')
            rows = self._bindings(conn, actor, kb_id)
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
                      'query_blocks_implementation_sha256': sha256(Path(evidence_packing.__file__).read_bytes()).hexdigest(),
                      'tie_break': list(TIE_BREAK_FIELDS),
                      'embedding': self.embedding_provider.manifest if model_enabled else None,
                      'vector_source_ids': sorted(vector_sources),
                      'excluded_vector_source_ids': sorted({row['source_id'] for row in rows} - vector_sources)
                      if model_enabled else [],
                      'model_admission': 'EXPERIMENTAL' if model_enabled else 'NOT_ENABLED'}
            conn.execute('INSERT INTO builds VALUES (%s,%s,%s,%s,%s,%s,%s,%s,\'STAGING\',%s)',
                         (actor.tenant_id, kb_id, build_id, kb['manifest_id'], authority_revision,
                          kb['revision'], index, Jsonb(config), len(units)))
            for citation in citations:
                conn.execute('INSERT INTO citations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', citation)
        try:
            if model_enabled:
                with self._tx(actor, kb_id) as conn:
                    if self._lock_authority(conn, actor) != authority_revision:
                        raise PlatformError('STALE_BUILD', 'Authorization changed before model processing', 409)
                    self._auth(conn, actor, kb_id, 'kb.manage')
                    eligible = [unit for unit in units if unit['source_id'] in vector_sources and unit['kind'] != 'Page']
                    vectors = self.embedding_provider.documents([unit['text'] for unit in eligible])
                    for unit, vector in zip(eligible, vectors, strict=True):
                        unit['vector'] = vector
                self.backend.stage(index, units, vector_dimension=self.embedding_provider.dimension)
            else:
                self.backend.stage(index, units)
        except (SearchUnavailable, EmbeddingError, PlatformError) as exc:
            with self._tx(actor, kb_id) as conn:
                conn.execute('UPDATE builds SET status=\'FAILED\' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s',
                             (actor.tenant_id, kb_id, build_id))
            if isinstance(exc, PlatformError):
                raise
            if isinstance(exc, EmbeddingError):
                raise PlatformError('EMBEDDING_FAILED', str(exc), 422) from exc
            raise PlatformError('SEARCH_UNAVAILABLE', str(exc), 503) from exc
        with self._tx(actor, kb_id) as conn:
            conn.execute('UPDATE builds SET status=\'VALIDATED\' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s',
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

    def _embedding_allowed(self, row):
        return (self.embedding_provider is not None and row['policy'].get('embedding', False) and
                self.embedding_provider.provider_id in row['policy'].get('allowed_providers', []))

    @staticmethod
    def _build_config(build):
        if isinstance(build['inputs'], dict):
            return build['inputs']
        return {'channels': ['bm25', 'structure', 'table'], 'embedding': None, 'vector_source_ids': []}

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
                                  Jsonb(evidence_ids), Jsonb(unit_anchors), Jsonb(element.get('table'))))
        return units, citations

    def publish(self, actor, kb_id, build_id):
        with self._tx(actor, kb_id) as conn:
            authority_revision = self._lock_authority(conn, actor)
            kb = self._auth(conn, actor, kb_id, 'kb.manage')
            build = conn.execute('SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s FOR UPDATE',
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
            conn.execute('UPDATE knowledge_bases SET manifest_id=%s WHERE tenant_id=%s AND kb_id=%s',
                         (build_id, actor.tenant_id, kb_id))
            conn.execute('UPDATE builds SET status=\'PUBLISHED\' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s',
                         (actor.tenant_id, kb_id, build_id))
        return {'kb_id': kb_id, 'manifest_id': build_id, 'status': 'PUBLISHED', 'idempotent': False}

    def search(self, actor, kb_id, query, channel='bm25', top_k=5, context_budget=4000, purpose='human_search'):
        if channel not in {'bm25', 'structure', 'table', 'dense', 'hybrid'}:
            raise PlatformError('CHANNEL_NOT_ADMITTED', 'Channel has no approved configured provider', 409)
        if not query.strip() or len(query) > 2000 or not 1 <= top_k <= 50 or not 1 <= context_budget <= 50000:
            raise PlatformError('INVALID_QUERY', 'Invalid query or retrieval budget')
        with self._tx(actor, kb_id) as conn:
            kb = self._auth(conn, actor, kb_id)
            revision = self._revision(conn, actor)
            allowed = self._allowed(self._bindings(conn, actor, kb_id), actor, purpose)
            if not kb['manifest_id']:
                raise PlatformError('NO_PUBLISHED_MANIFEST', 'Knowledge base has no published index', 409)
            build = conn.execute('SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s '
                                 'AND status=\'PUBLISHED\'', (actor.tenant_id, kb_id, kb['manifest_id'])).fetchone()
            if not build:
                raise PlatformError('INVALID_MANIFEST', 'Published index unavailable', 503)
            config = self._build_config(build)
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
                with self._tx(actor, kb_id) as conn:
                    if self._lock_authority(conn, actor) != revision:
                        raise PlatformError('SCOPE_CHANGED', 'Authorization changed before model processing', 409)
                    self._auth(conn, actor, kb_id)
                    vector = self.embedding_provider.query(query)
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
                record = conn.execute('SELECT * FROM citations WHERE tenant_id=%s AND kb_id=%s AND citation_id=%s '
                                      'AND build_id=%s', (actor.tenant_id, kb_id, hit.get('citation_id'),
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

    def resolve_citation(self, actor, kb_id, citation_id, purpose='preview'):
        if purpose not in {'preview', 'download', 'llm_context'}:
            raise PlatformError('INVALID_PURPOSE', 'Unknown citation purpose')
        with self._tx(actor, kb_id) as conn:
            self._lock_authority(conn, actor)
            self._auth(conn, actor, kb_id)
            row = conn.execute('SELECT c.*,p.document,v.filename,v.original_path,v.original_sha256,v.media_type,s.policy,'
                               's.owner_id,s.readers FROM citations c JOIN bindings b '
                               'USING (tenant_id,kb_id,binding_id) JOIN builds d ON c.tenant_id=d.tenant_id '
                               'AND c.kb_id=d.kb_id AND c.build_id=d.build_id '
                               'JOIN source_versions v ON c.tenant_id=v.tenant_id AND c.source_id=v.source_id '
                               'AND c.source_version_id=v.source_version_id '
                               'JOIN sources s ON c.tenant_id=s.tenant_id AND c.source_id=s.source_id '
                               'JOIN parse_snapshots p ON c.tenant_id=p.tenant_id AND c.source_id=p.source_id '
                               'AND c.source_version_id=p.source_version_id AND c.parse_snapshot_id=p.parse_snapshot_id '
                               'WHERE c.tenant_id=%s AND c.kb_id=%s AND c.citation_id=%s '
                               'AND b.source_id=c.source_id '
                               'AND b.status=\'ACTIVE\' AND d.status=\'PUBLISHED\'',
                               (actor.tenant_id, kb_id, citation_id)).fetchone()
            if not row or not self._allowed([row], actor, purpose):
                _deny()
            try:
                self.store.checked_path(row['original_path'], actor.tenant_id, row['original_sha256'])
            except (OSError, ValueError) as exc:
                raise PlatformError('SOURCE_INTEGRITY', 'Original integrity check failed', 503) from exc
            return {key: row[key] for key in ('citation_id', 'kb_id', 'source_id', 'source_version_id', 'filename',
                    'original_path', 'original_sha256', 'text', 'anchors', 'media_type', 'policy', 'table_data',
                    'build_id', 'parse_snapshot_id', 'evidence_ids')}

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

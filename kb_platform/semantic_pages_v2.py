"""Explicitly built, sealed canonical Page vectors and authorized dense queries.

No constructor builds an index or embeds text. Capsules are imported only by an
explicit managing build; queries use the formal index, never an experiment DB.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from .agent_tools import EvidenceGateway, _policy, evidence_record
from .core import PlatformError
from .delivery_units import PAGE_PROTOCOL, indexed_elements
from .rag_testing.contracts import digest
from .search import BINDING_FIELDS, OpenSearch, SearchUnavailable, score_order

PROTOCOL = 'authorized-canonical-semantic-pages/2'
QUERY_PROTOCOL = 'filtered-native-cosine-page-knn/1'


class SemanticPageResults(list):
    """Canonical records plus the independently auditable zero-hit receipt."""
    def __init__(self, records, receipt):
        super().__init__(records)
        self.receipt = receipt


def _sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def _json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _vector(value, dimension):
    if (not isinstance(value, (list, tuple)) or len(value) != dimension or
            any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in value)):
        raise PlatformError('INVALID_PAGE_VECTOR', 'A finite normalized Page vector is required', 422)
    result = [float(v) for v in value]
    if not math.isclose(math.sqrt(sum(v*v for v in result)), 1.0, abs_tol=1e-4):
        raise PlatformError('INVALID_PAGE_VECTOR', 'Page vectors must be L2 normalized', 422)
    return result


def _page_identity(page):
    unit = page['unit']
    return {'page_index': page['page_index'], 'start': page['start'], 'end': page['end'],
        'citation_id': unit['citation_id'], 'text_sha256': hashlib.sha256(unit['text'].encode()).hexdigest(),
        'canonical_record_sha256': digest({k:unit[k] for k in
            ('text','anchors','evidence_ids',*BINDING_FIELDS,'build_id','citation_id')})}


class SemanticPageAuthority:
    """Hold the normal tenant authority lock throughout permitted processing."""
    def __init__(self, platform):
        self.platform = platform

    @contextmanager
    def locked(self, actor, kb_id, *, manage=False):
        with self.platform._tx(actor, kb_id) as conn:
            revision = self.platform._lock_authority(conn, actor)
            kb = self.platform._auth(conn, actor, kb_id, 'kb.manage' if manage else 'kb.read')
            self.platform._auth(conn, actor, kb_id, 'kb.read')
            if not kb['manifest_id']:
                raise PlatformError('NO_PUBLISHED_MANIFEST', 'Publish canonical Pages first', 409)
            build = conn.execute('SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s '
                "AND status='PUBLISHED'", (actor.tenant_id, kb_id, kb['manifest_id'])).fetchone()
            if not build or self.platform._build_config(build).get('page_navigation_protocol') != PAGE_PROTOCOL:
                raise PlatformError('SEMANTIC_PAGE_BUILD_REQUIRED', 'The current build has no canonical Page channel', 409)
            rows = self.platform._allowed(self.platform._bindings(conn, actor, kb_id), actor, 'human_search')
            state = {'scope_id': kb_id, 'manifest_id': kb['manifest_id'], 'authority_revision': revision}
            def authorize():
                current = self.platform._auth(conn, actor, kb_id)
                return current['manifest_id'] == state['manifest_id'] and self.platform._revision(conn, actor) == revision
            yield {'state': state, 'rows': rows, 'build': build, 'conn': conn, 'authorize': authorize}

    def catalog(self, actor, kb_id, snapshot, rows):
        """Derive current Page ids/ranges and verify original bytes + citation DB."""
        platform, conn = self.platform, snapshot['conn']
        build_id = snapshot['state']['manifest_id']
        units, _ = platform._units(actor, kb_id, build_id, rows)
        by_id = {u['citation_id']: u for u in units if u['kind'] == 'Page'}
        stored = conn.execute('SELECT * FROM citations WHERE tenant_id=%s AND kb_id=%s AND build_id=%s',
                              (actor.tenant_id, kb_id, build_id)).fetchall()
        stored = {r['citation_id']: r for r in stored}
        result = {}
        for row in rows:
            doc = row['document']
            if doc['status'] != 'READY' or doc['media_type'] != 'application/pdf':
                raise PlatformError('SEMANTIC_PAGE_SOURCE_INVALID', 'Complete canonical PDF Pages are required', 409)
            if [e['ordinal'] for e in doc['elements']] != sorted(e['ordinal'] for e in doc['elements']):
                raise PlatformError('SEMANTIC_PAGE_SOURCE_INVALID', 'Canonical parse order changed', 503)
            text = '\n'.join(e['raw_text'] for e in doc['elements'])
            original = conn.execute('SELECT original_path,original_sha256 FROM source_versions '
                'WHERE tenant_id=%s AND source_id=%s AND source_version_id=%s',
                (actor.tenant_id, row['source_id'], row['source_version_id'])).fetchone()
            if not original:
                raise PlatformError('SOURCE_INTEGRITY', 'Original source version is unavailable', 503)
            try:
                platform.store.checked_path(original['original_path'], actor.tenant_id, original['original_sha256'])
            except (ValueError, OSError) as exc:
                raise PlatformError('SOURCE_INTEGRITY', 'Original source integrity failed', 503) from exc
            elements = {e['element_id']: e for e in indexed_elements(doc, include_pages=True) if e['kind'] == 'Page'}
            pages = []
            for unit in by_id.values():
                if unit['binding_id'] != row['binding_id']:
                    continue
                citation = stored.get(unit['citation_id'])
                if not citation or any(citation.get(k) != unit[k] for k in (*BINDING_FIELDS, 'build_id', 'text', 'anchors', 'evidence_ids')):
                    raise PlatformError('SEMANTIC_PAGE_SOURCE_INVALID', 'Page citation differs from current canonical data', 503)
                element = elements[citation['element_id']]
                start, end = element['parse_text_start'], element['parse_text_end']
                indices = {a['page_index'] for a in unit['anchors']}
                if len(indices) != 1 or text[start:end] != unit['text']:
                    raise PlatformError('SEMANTIC_PAGE_SOURCE_INVALID', 'Page range or original text changed', 503)
                pages.append({'page_index': next(iter(indices)), 'start': start, 'end': end, 'text': unit['text'], 'unit': unit})
            pages.sort(key=lambda p: p['page_index'])
            if not pages or len({p['page_index'] for p in pages}) != len(pages):
                raise PlatformError('SEMANTIC_PAGE_SOURCE_INVALID', 'Page inventory is incomplete or ambiguous', 503)
            result[row['source_id']] = {'source_identity': {'source_id': row['source_id'],
                'source_version_id': row['source_version_id'], 'parse_snapshot_id': row['parse_snapshot_id'],
                'source_text_sha256': hashlib.sha256(text.encode()).hexdigest()}, 'source_text': text, 'pages': pages,
                'original_sha256': original['original_sha256'], 'binding': {k: row[k] for k in BINDING_FIELDS},
                'source_revision': row['source_revision'], 'binding_revision': row['revision'], 'policy_sha256': digest(row['policy'])}
        return result


def dense_page_query(backend, index, actor, kb_id, build_id, allowed, vector, top_k, excluded):
    """All restrictions are in the KNN filter before approximate TopK."""
    if not allowed:
        return []
    filters = backend._filters(actor.tenant_id, kb_id, allowed, compact=True)
    filters += [{'term': {'kind': 'Page'}}, {'term': {'build_id': build_id}}]
    restriction = {'filter': filters}
    if excluded:
        restriction['must_not'] = [{'terms': {'citation_id': list(excluded)}}]
    body = {'size': top_k, '_source': {'excludes': ['vector']}, 'sort': score_order(),
        'query': {'knn': {'vector': {'vector': vector, 'k': top_k, 'filter': {'bool': restriction}}}}}
    response = backend._request('POST', f'/{OpenSearch._index(index)}/_search', json=body)
    result = []
    for hit in response['hits']['hits']:
        row = {**hit['_source'], 'score': hit['_score']}
        row['text'] = row.pop('original_page_text')
        row.pop('page_text', None)
        row['heading_text'] = row.pop('page_heading', '')
        result.append(row)
    return result


class SemanticPageService:
    def __init__(self, platform, provider, storage_root=Path('.local/semantic-pages'), *, gateway=None, authority=None):
        manifest=provider.manifest
        if (not isinstance(provider.provider_id,str) or not provider.provider_id or len(provider.provider_id)>100 or
                type(provider.dimension) is not int or not 1<=provider.dimension<=65536 or
                not isinstance(manifest,dict) or manifest.get('provider')!=provider.provider_id or
                manifest.get('dimension')!=provider.dimension or type(manifest.get('max_tokens')) is not int or
                manifest['max_tokens']<1):
            raise ValueError('A fixed provider identity, dimension and native input limit are required; no fallback')
        self.platform, self.provider = platform, provider
        self.root = Path(storage_root).resolve()
        self.gateway = gateway or EvidenceGateway(platform)
        self.authority = authority or SemanticPageAuthority(platform)

    def _directory(self, actor, kb_id):
        return self.root / digest([actor.tenant_id, kb_id])

    def _processing(self,rows):
        if not rows or any(not r['policy'].get('embedding') or self.provider.provider_id not in r['policy'].get('allowed_providers', []) for r in rows):
            raise PlatformError('PROCESSING_DENIED', 'Current source policy must explicitly authorize the declared embedding provider', 403)

    def _dependencies(self):
        from . import delivery_units, search
        return {str(Path(p).name): _sha(p) for p in (__file__, delivery_units.__file__, search.__file__)}

    def current_manifest(self, actor, kb_id):
        state = self.gateway.authorize(actor, kb_id)
        try:
            directory = self._directory(actor, kb_id)
            pointer = json.loads((directory/'active.json').read_text('utf-8'))
            if Path(pointer['manifest']).name != pointer['manifest']:
                raise ValueError('manifest path')
            path = directory/pointer['manifest']
            if _sha(path) != pointer['sha256']:
                raise ValueError('manifest checksum')
            manifest = json.loads(path.read_text('utf-8'))
            if manifest['tenant_id'] != actor.tenant_id or manifest['kb_id'] != kb_id or manifest['protocol'] != PROTOCOL:
                raise ValueError('manifest identity')
            if (manifest['embedding'] != self.provider.manifest or manifest['dependencies'] != self._dependencies()
                    or manifest['provider_id'] != self.provider.provider_id or manifest['dimension'] != self.provider.dimension):
                raise PlatformError('SEMANTIC_PAGE_MODEL_MISMATCH',
                    'Semantic Page dependencies changed; there is no provider fallback', 409)
            if (manifest['build_id'] != state['manifest_id']
                    or manifest['authority_revision'] != state['authority_revision']):
                raise PlatformError('SEMANTIC_PAGE_INDEX_STALE',
                    'Build or authority changed; explicitly rebuild semantic Pages', 409)
            return manifest, pointer['sha256']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PlatformError('SEMANTIC_PAGE_INDEX_UNAVAILABLE', 'Build a verified semantic Page index explicitly', 409) from exc

    def _index_bound(self, manifest, checksum):
        index = OpenSearch._index(manifest['index_name'])
        mapping = self.platform.backend._request('GET', f'/{index}/_mapping')
        settings = self.platform.backend._request('GET', f'/{index}/_settings')
        if (mapping.get(index, {}).get('mappings', {}).get('_meta', {}).get('semantic_manifest_sha256') != checksum or
                str(settings.get(index, {}).get('settings', {}).get('index', {}).get('blocks', {}).get('write')).lower() != 'true'):
            raise PlatformError('SEMANTIC_PAGE_INDEX_INVALID', 'Semantic index is unsealed or has a different manifest', 503)

    def build(self, actor, kb_id, capsules, *, expected_state=None):
        """Explicit mutation: root/admin calls only after processing approval."""
        if not isinstance(capsules, dict) or not capsules or any(not isinstance(k, str) or not isinstance(v, dict) or set(v) != {'path', 'sha256'} for k,v in capsules.items()):
            raise PlatformError('INVALID_PAGE_CAPSULES', 'Provide source-keyed capsules with trusted external SHA256', 422)
        with self.authority.locked(actor, kb_id, manage=True) as snapshot:
            if expected_state is not None and snapshot['state'] != expected_state:
                raise PlatformError('SEMANTIC_PAGE_BUILD_STALE','Build or authority differs from the frozen build plan',409)
            rows = [r for r in snapshot['rows'] if r['source_id'] in capsules]
            if len(rows) != len(capsules) or {r['source_id'] for r in rows} != set(capsules):
                raise PlatformError('ACCESS_DENIED', 'All capsule sources must be uniquely bound and currently readable', 403)
            self._processing(rows)
            catalog = self.authority.catalog(actor, kb_id, snapshot, rows)
            units, sources, receipts = [], [], []
            for source_id in sorted(catalog):
                item = catalog[source_id]
                pages = [{k:p[k] for k in ('page_index','start','end','text')} for p in item['pages']]
                capsule = capsules[source_id]
                imported = self.provider.import_capsule(capsule['path'], expected_sha256=capsule['sha256'],
                    source_identity=item['source_identity'], pages=pages, source_text=item['source_text'], authorize=snapshot['authorize'])
                if len(imported.vectors) != len(pages) or imported.manifest.get('new_embedding_calls')!=0:
                    raise PlatformError('SEMANTIC_CAPSULE_INVALID', 'Capsule Page count differs from current canonical inventory', 503)
                bindings = []
                for page, raw in zip(item['pages'], imported.vectors, strict=True):
                    vector = _vector(raw,self.provider.dimension)
                    unit = {**page['unit'], 'vector': vector, 'semantic_vector_sha256': digest(vector),
                        'page_index': page['page_index'], 'parse_text_start': page['start'], 'parse_text_end': page['end']}
                    units.append(unit)
                    bindings.append(_page_identity(page) | {'vector_sha256':digest(vector)})
                sources.append({k:v for k,v in item.items() if k not in ('source_text','pages')} | {'ordered_pages':bindings})
                receipts.append({'source_id':source_id,'capsule_sha256':capsule['sha256'],'import_manifest':imported.manifest})
            if not snapshot['authorize']():
                raise PlatformError('SCOPE_CHANGED', 'Authorization changed before vector import', 409)
            identity = uuid4().hex
            index = self.platform.backend.index_name(actor.tenant_id, kb_id, identity)
            manifest = {'protocol':PROTOCOL,'query_protocol':QUERY_PROTOCOL,'index_name':index,
                'tenant_id':actor.tenant_id,'kb_id':kb_id,'build_id':snapshot['state']['manifest_id'],
                'authority_revision':snapshot['state']['authority_revision'],'built_by_principal':actor.principal_id,
                'embedding':deepcopy(self.provider.manifest),'provider_id':self.provider.provider_id,
                'dimension':self.provider.dimension,'sources':sources,
                'ordered_page_ids':[u['citation_id'] for u in units],'ordered_vectors_sha256':digest([u['vector'] for u in units]),
                'capsule_imports':receipts,'dependencies':self._dependencies(),'new_document_embedding_calls':0,
                'canonical_page_protocol':PAGE_PROTOCOL,'index_mutability':'write blocked after exact readback'}
            self.platform.backend.stage(index, units, vector_dimension=self.provider.dimension)
            response = self.platform.backend._request('POST', f'/{index}/_mget', json={'ids':manifest['ordered_page_ids']})
            stored = {r['_id']:r for r in response['docs'] if r.get('found')}
            if set(stored) != set(manifest['ordered_page_ids']):
                raise PlatformError('SEMANTIC_PAGE_INDEX_INVALID', 'Vector index readback is incomplete', 503)
            for unit in units:
                data = stored[unit['citation_id']]['_source']
                if (data.get('original_page_text') != unit['text'] or data.get('vector') != unit['vector'] or
                        any(data.get(k) != unit[k] for k in (*BINDING_FIELDS,'build_id','kind','citation_id','anchors','evidence_ids'))):
                    raise PlatformError('SEMANTIC_PAGE_INDEX_INVALID', 'Vector index differs from the imported original Page', 503)
            if not snapshot['authorize']():
                raise PlatformError('SCOPE_CHANGED', 'Authorization changed during vector import', 409)
            directory = self._directory(actor, kb_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory/(identity+'.json')
            _json(path, manifest)
            checksum = _sha(path)
            self.platform.backend._request('PUT', f'/{index}/_mapping', json={'_meta':{'semantic_manifest_sha256':checksum}})
            self.platform.backend._request('PUT', f'/{index}/_settings', json={'index.blocks.write':True})
            self._index_bound(manifest,checksum)
            if not snapshot['authorize']():
                raise PlatformError('SCOPE_CHANGED', 'Authorization changed before semantic publication', 409)
            temporary=directory/('active-'+uuid4().hex+'.tmp')
            _json(temporary,{'manifest':path.name,'sha256':checksum})
            os.replace(temporary,directory/'active.json')
            return {'manifest_path':str(path),'manifest_sha256':checksum,**manifest}

    def query(self, actor, kb_id, query, policy, limit=5, *, exclude_citation_ids=(), call_context=None):
        policy = _policy(policy)
        if (not isinstance(query,str) or not query.strip() or len(query)>2000 or type(limit) is not int or not 1<=limit<=20 or
                not isinstance(exclude_citation_ids,(tuple,list)) or len(exclude_citation_ids)>50 or
                any(not isinstance(x,str) or not x or len(x)>512 for x in exclude_citation_ids) or len(set(exclude_citation_ids))!=len(exclude_citation_ids)):
            raise PlatformError('INVALID_QUERY','Invalid semantic Page query or exclusions',422)
        if (not isinstance(call_context,dict) or
                any(not isinstance(call_context.get(k),str) or not call_context[k] or len(call_context[k])>200
                    for k in ('session_id','turn_id')) or
                any(not isinstance(k,str) or not isinstance(v,str) or len(k)>100 or len(v)>500
                    for k,v in call_context.items()) or len(call_context)>12):
            raise PlatformError('INVALID_CALL_CONTEXT','Semantic query requires explicit session and turn audit identity',422)
        manifest, checksum = self.current_manifest(actor,kb_id)
        state, bindings, _ = self.gateway._state(actor,kb_id,with_build=True)
        allowed = self.gateway._allowed(actor,bindings,policy)
        if policy['version_policy']=='pinned':
            if any(r['source_id'] in policy['versions'] and r['source_version_id']!=policy['versions'][r['source_id']] for r in allowed):
                raise PlatformError('PINNED_VERSION_NOT_INDEXED','Use saved citations for historical evidence',409)
            allowed=[r for r in allowed if policy['versions'].get(r['source_id'])==r['source_version_id']]
        with self.authority.locked(actor,kb_id) as snapshot:
            if snapshot['state']!=state or manifest['build_id']!=state['manifest_id'] or manifest['authority_revision']!=state['authority_revision']:
                raise PlatformError('SEMANTIC_PAGE_INDEX_STALE','Build or authority changed; explicitly rebuild semantic Pages',409)
            if (manifest['embedding']!=self.provider.manifest or manifest['dependencies']!=self._dependencies() or
                    manifest['provider_id']!=self.provider.provider_id or manifest['dimension']!=self.provider.dimension):
                raise PlatformError('SEMANTIC_PAGE_MODEL_MISMATCH','Semantic Page dependencies changed; there is no provider fallback',409)
            represented={s['source_identity']['source_id']:s for s in manifest['sources']}
            allowed=[r for r in allowed if r['source_id'] in represented]
            request_id,vector,input_tokens=None,None,None
            if allowed:
                allowed_keys={tuple(a[k] for k in BINDING_FIELDS) for a in allowed}
                current=[r for r in snapshot['rows'] if tuple(r[k] for k in BINDING_FIELDS) in allowed_keys]
                if {tuple(r[k] for k in BINDING_FIELDS) for r in current} != allowed_keys:
                    raise PlatformError('SCOPE_CHANGED','Current authorized bindings changed before embedding',409)
                self._processing(current)
                catalog=self.authority.catalog(actor,kb_id,snapshot,current)
                for sid,item in catalog.items():
                    expected=represented[sid]
                    if (any(item[k]!=expected[k] for k in ('source_identity','binding','source_revision','binding_revision','policy_sha256','original_sha256')) or
                            [_page_identity(p) for p in item['pages']] !=
                            [{k:v for k,v in p.items() if k!='vector_sha256'} for p in expected['ordered_pages']]):
                        raise PlatformError('SEMANTIC_PAGE_INDEX_STALE','Canonical source or Page inventory changed',409)
                try:
                    self._index_bound(manifest,checksum)
                except SearchUnavailable as exc:
                    raise PlatformError('SEARCH_UNAVAILABLE','Semantic Page index unavailable; no fallback',503) from exc
                identities=[catalog[sid]['source_identity'] for sid in sorted(catalog)]
                audit={'tenant_id':actor.tenant_id,'kb_id':kb_id,'build_id':state['manifest_id'],'principal':actor.principal_id,
                    'acl_revision':str(state['authority_revision']),'sources':identities,'semantic_manifest_sha256':checksum,
                    'call_context':deepcopy(call_context)}
                for field in ('source_id','source_version_id','parse_snapshot_id','source_text_sha256'):
                    audit[field]=identities[0][field] if len(identities)==1 else [x[field] for x in identities]
                request_id='semantic-page-query-'+uuid4().hex
                observation=self.provider.query_observed(query,request_id=request_id,audit_context=audit,authorize=snapshot['authorize'])
                vector=_vector(observation['vector'],self.provider.dimension)
                if (observation.get('physical_request_id')!=request_id or observation.get('vector_sha256')!=digest(vector) or
                        observation.get('text_sha256')!=[hashlib.sha256(query.encode()).hexdigest()] or
                        observation.get('manifest_sha256')!=digest(self.provider.manifest) or
                        type(observation.get('input_tokens')) is not int or not 0<observation['input_tokens']<=self.provider.manifest['max_tokens']):
                    raise PlatformError('SEMANTIC_PAGE_QUERY_INVALID','Embedding observation is not bound to the physical query',503)
                input_tokens=observation['input_tokens']
        groups=[allowed]
        if policy['mode']=='PREFER' and policy['source_ids']:
            groups=[[r for r in allowed if r['source_id'] in policy['source_ids']], [r for r in allowed if r['source_id'] not in policy['source_ids']]]
        records=[]
        try:
            for group in groups:
                candidates=dense_page_query(self.platform.backend,manifest['index_name'],actor,kb_id,state['manifest_id'],group,vector,limit-len(records),exclude_citation_ids) if group and len(records)<limit else []
                keys={tuple(r[k] for k in BINDING_FIELDS) for r in group}
                if len(candidates)>limit-len(records):
                    raise PlatformError('INDEX_SCOPE_VIOLATION','Semantic index exceeded candidate limit',503)
                for candidate in candidates:
                    if (candidate.get('tenant_id')!=actor.tenant_id or candidate.get('kb_id')!=kb_id or candidate.get('build_id')!=state['manifest_id'] or
                            candidate.get('kind')!='Page' or candidate.get('citation_id') in exclude_citation_ids or
                            candidate.get('unit_id')!=candidate.get('citation_id') or tuple(candidate.get(k) for k in BINDING_FIELDS) not in keys):
                        raise PlatformError('INDEX_SCOPE_VIOLATION','Semantic hit is outside authorized canonical Pages',503)
                    canonical=self.gateway._read_one(actor,kb_id,candidate['citation_id'],policy,state,bindings)
                    if any(candidate.get(k)!=canonical[k] for k in ('source_id','source_version_id','parse_snapshot_id','build_id','text','anchors','evidence_ids')):
                        raise PlatformError('INDEX_EVIDENCE_VIOLATION','Semantic Page differs from original citation',503)
                    score=candidate.get('score')
                    if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or any(r['citation_id']==canonical['citation_id'] for r in records):
                        raise PlatformError('INDEX_EVIDENCE_VIOLATION','Invalid score or duplicate semantic citation',503)
                    records.append(evidence_record(canonical)|{'score':score,'retrieval_method':'authorized_page_dense',
                        'embedding_provider_id':self.provider.provider_id,
                        'kind':'Page','embedding_request_id':request_id,'query_vector_sha256':digest(vector),'semantic_manifest_sha256':checksum})
        except SearchUnavailable as exc:
            raise PlatformError('SEARCH_UNAVAILABLE','Semantic Page index unavailable; no fallback',503) from exc
        self.gateway._unchanged(actor,kb_id,state)
        return SemanticPageResults(records, {'embedding_request_id':request_id,'query_vector_sha256':digest(vector) if vector else None,
            'semantic_manifest_sha256':checksum,'retrieval_method':'authorized_page_dense',
            'embedding_provider_id':self.provider.provider_id,
            'embedding_input_tokens':input_tokens,'build_id':state['manifest_id'],
            'authority_revision':state['authority_revision'],'call_context':deepcopy(call_context)})

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .core import PlatformError
from .delivery_units import indexed_elements
from .embeddings import EmbeddingError
from .search import BINDING_FIELDS, SearchUnavailable


PROTOCOL = 'authorized-child-fusion-rerank-parent/1'


@dataclass(frozen=True)
class PipelineConfig:
    channels: tuple[str, ...] = ('bm25',)
    candidate_limit: int = 10
    fused_limit: int = 20
    rerank: bool = False
    parent: str = 'paragraph'
    top_k: int = 5
    max_expanded_units: int = 50
    rrf_k: int = 60
    title_navigation: bool = True
    @classmethod
    def parse(cls, value=None):
        if isinstance(value, cls):
            config = value
        else:
            if value is not None and (not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__)):
                raise PlatformError('INVALID_PIPELINE_CONFIG', 'Unknown retrieval configuration', 422)
            data = dict(value or {})
            if isinstance(data.get('channels'), list):
                data['channels'] = tuple(data['channels'])
            try:
                config = cls(**data)
            except TypeError as exc:
                raise PlatformError('INVALID_PIPELINE_CONFIG', 'Invalid retrieval configuration', 422) from exc
        if (not isinstance(config.channels, tuple) or not config.channels or
                any(channel not in ('bm25', 'dense') for channel in config.channels) or
                len(set(config.channels)) != len(config.channels) or
                type(config.rerank) is not bool or type(config.title_navigation) is not bool or
                config.parent not in ('none', 'paragraph', 'section', 'page') or
                any(type(value) is not int for value in (config.candidate_limit, config.fused_limit,
                    config.top_k, config.max_expanded_units, config.rrf_k)) or
                not 1 <= config.candidate_limit <= 20 or not 1 <= config.fused_limit <= 20 or
                not 1 <= config.top_k <= 20 or not 1 <= config.max_expanded_units <= 50 or
                not 1 <= config.rrf_k <= 1000):
            raise PlatformError('INVALID_PIPELINE_CONFIG', 'Invalid retrieval configuration or limits', 422)
        return config


class LunaCandidateReranker:


    def __init__(self, luna):
        self.luna = luna
        self.provider_id = luna.provider_id
        self.manifest = luna.manifest

    def bind_turn(self, completion):
        return type(self)(self.luna.bind_turn(completion))

    def rank(self, query, candidates):
        from .reranking import rerank_documents

        ranking, _ = rerank_documents(self.luna, query,
            [{'doc_id': item['citation_id'], 'text': item['text']} for item in candidates])
        return [{'citation_id': item['doc_id'], 'score': item['score']} for item in ranking]


def _allowed_bindings(gateway, actor, bindings, policy):
    allowed = gateway._allowed(actor, bindings, policy)
    if policy['version_policy'] == 'pinned':
        if any(row['source_id'] in policy['versions'] and
               row['source_version_id'] != policy['versions'][row['source_id']] for row in allowed):
            raise PlatformError('PINNED_VERSION_NOT_INDEXED', 'Use saved citations for historical versions', 409)
        allowed = [row for row in allowed if policy['versions'].get(row['source_id']) == row['source_version_id']]
    return allowed


def _catalog(gateway, actor, scope_id, state, allowed, *, mapping=None):

    allowed_rows = {tuple(row[key] for key in BINDING_FIELDS): row for row in allowed}
    allowed_keys = set(allowed_rows)
    with gateway.platform._tx(actor, scope_id) as conn:
        gateway.platform._lock_authority(conn, actor)
        gateway.platform._auth(conn, actor, scope_id)
        rows = [row for row in gateway.platform._bindings(conn, actor, scope_id)
                if tuple(row[key] for key in BINDING_FIELDS) in allowed_keys]
    gateway._unchanged(actor, scope_id, state)
    documents, units = [], {}
    if mapping is None:
        mapping = gateway._mapping(actor, scope_id)
    for row in rows:
        row = {**allowed_rows[tuple(row[key] for key in BINDING_FIELDS)], **row}
        document = row['document']
        cursor, positions = 0, {}
        for element in document['elements']:
            positions[element['element_id']] = (cursor, cursor + len(element['raw_text']))
            cursor += len(element['raw_text']) + 1
        metadata = gateway._registered(mapping, row['source_id'])
        text = '\n'.join(element['raw_text'] for element in document['elements'])
        source = {**row, 'text': text, 'positions': positions, 'doc_id': metadata['doc_id'],
                  'title': metadata.get('title') or Path(row.get('filename', row['source_id'])).stem}
        documents.append(source)
        elements = indexed_elements(document, include_pages=True)
        ids = {element['element_id']: str(uuid5(NAMESPACE_URL,
               f'{state["manifest_id"]}/{row["binding_id"]}/{element["element_id"]}')) for element in elements}
        for element in elements:
            if not element.get('raw_text', '').strip():
                continue
            left, right = (element['parse_text_start'], element['parse_text_end']) if 'parse_text_start' in element else positions[element['element_id']]
            citation_id = ids[element['element_id']]
            units[citation_id] = {'citation_id': citation_id, 'source': source,
                'element': element, 'start': left, 'end': right,
                'parent_id': ids.get(element.get('parent_id'))}
    return documents, units


def _check_candidates(gateway, actor, scope_id, candidates, policy, state, bindings, allowed, *, limit):
    from .agent_tools import EVIDENCE_FIELDS

    allowed_keys = {tuple(row[key] for key in BINDING_FIELDS) for row in allowed}
    if not isinstance(candidates, list) or len(candidates) > limit:
        raise PlatformError('INDEX_SCOPE_VIOLATION', 'Index exceeded the candidate budget', 503)
    records, seen = [], set()
    for hit in candidates:
        if (not isinstance(hit, dict) or hit.get('tenant_id') != actor.tenant_id or
                hit.get('kb_id') != scope_id or hit.get('build_id') != state['manifest_id'] or
                hit.get('kind') == 'Page' or tuple(hit.get(key) for key in BINDING_FIELDS) not in allowed_keys or
                not isinstance(hit.get('citation_id'), str) or hit.get('unit_id') != hit['citation_id'] or
                hit['citation_id'] in seen):
            raise PlatformError('INDEX_SCOPE_VIOLATION', 'Candidate is outside authorized child units', 503)
        canonical = gateway._read_one(actor, scope_id, hit['citation_id'], policy, state, bindings)
        if any(hit.get(key) != canonical[key] for key in
               ('source_id', 'source_version_id', 'parse_snapshot_id', 'build_id', 'text', 'anchors', 'evidence_ids')):
            raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Candidate differs from original evidence', 503)
        score = hit.get('score')
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Candidate score is invalid', 503)
        seen.add(hit['citation_id'])
        records.append({key: canonical[key] for key in EVIDENCE_FIELDS} | {'score': float(score)})
    return records


def _parents(seed, units, mode):
    if mode == 'none':
        return [seed['citation_id']]
    item = units[seed['citation_id']]
    source = item['source']
    siblings = [unit for unit in units.values() if unit['source']['binding_id'] == source['binding_id']]
    if mode == 'page':
        parents = [unit for unit in siblings if unit['element']['kind'] == 'Page' and
                   unit['start'] <= item['start'] and unit['end'] >= item['end']]
        if len(parents) != 1:
            raise PlatformError('PARENT_STRUCTURE_UNAVAILABLE', 'No unique original page for this format', 409)
        return [parents[0]['citation_id']]
    if mode == 'section':
        originals = source['document']['elements']
        original_ids = {element['element_id']: element for element in originals}
        members = [element for element in originals if
                   source['positions'][element['element_id']][0] < item['end'] and
                   source['positions'][element['element_id']][1] > item['start']]
        headings = {element['element_id'] if element['kind'] == 'Section' else element.get('parent_id')
                    for element in members}
        headings.discard(None)
        if len(headings) != 1:
            raise PlatformError('PARENT_STRUCTURE_UNAVAILABLE', 'No trusted heading section for this child', 409)
        heading = original_ids.get(next(iter(headings)))
        if heading is None or heading['kind'] != 'Section':
            raise PlatformError('PARENT_STRUCTURE_UNAVAILABLE', 'No trusted heading section for this child', 409)
        left = source['positions'][heading['element_id']][0]
        right = len(source['text'])
        for element in originals[originals.index(heading) + 1:]:
            if element['kind'] == 'Section' and element.get('heading_level', 1) <= heading.get('heading_level', 1):
                right = source['positions'][element['element_id']][0]
                break
        selected = [unit for unit in siblings if unit['element']['kind'] != 'Page' and
                    unit['start'] < right and unit['end'] > left]
    else:


        originals = [element for element in source['document']['elements']
                     if source['positions'][element['element_id']][0] < item['end'] and
                     source['positions'][element['element_id']][1] > item['start']]
        left = min(source['positions'][element['element_id']][0] for element in originals)
        right = max(source['positions'][element['element_id']][1] for element in originals)
        selected = [unit for unit in siblings if unit['element']['kind'] != 'Page' and
                    unit['start'] < right and unit['end'] > left]


    selected = [unit for unit in selected if unit['element']['kind'] != 'Table' or not any(
        other['citation_id'] != unit['citation_id'] and other['element']['kind'] != 'Table' and
        other['start'] <= unit['start'] and other['end'] >= unit['end'] for other in selected)]
    return [unit['citation_id'] for unit in sorted(selected, key=lambda unit: (unit['start'], unit['end'], unit['citation_id']))]


def execute(gateway, actor, scope_id, query, policy, config=None, *, exclude_citation_ids=()):
    from .agent_tools import EVIDENCE_FIELDS, _policy

    policy, config = _policy(policy), PipelineConfig.parse(config)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise PlatformError('INVALID_QUERY', 'Invalid query', 422)
    if not isinstance(exclude_citation_ids, (list, tuple)):
        raise PlatformError('INVALID_CITATIONS', 'Invalid excluded citations', 422)
    excluded = list(exclude_citation_ids)
    gateway._ids(excluded)
    if any(len(value) > 512 for value in excluded):
        raise PlatformError('INVALID_CITATIONS', 'Invalid excluded citations', 422)
    state, bindings, build = gateway._state(actor, scope_id, with_build=True)
    allowed = _allowed_bindings(gateway, actor, bindings, policy)
    build_config = build['inputs']
    provider = gateway.embedding_provider or getattr(gateway.platform, 'embedding_provider', None)
    vector_allowed = []
    if 'dense' in config.channels:
        if 'dense' not in build_config['channels']:
            raise PlatformError('CHANNEL_NOT_ADMITTED', 'Dense children were not published; build explicitly', 409)
        if provider is None or provider.manifest != build_config.get('embedding'):
            raise PlatformError('MODEL_VERSION_MISMATCH', 'Published child embedding is not configured', 409)
        vector_allowed = [row for row in allowed if row['policy'].get('embedding', False) and
            provider.provider_id in row['policy'].get('allowed_providers', []) and
            row['source_id'] in build_config.get('vector_source_ids', [])]
        if allowed and not vector_allowed:
            raise PlatformError('PROCESSING_NOT_ALLOWED', 'No authorized source admits this dense provider', 409)
    reranker = gateway.pipeline_reranker
    if config.rerank and reranker is None:
        raise PlatformError('RERANKER_NOT_CONFIGURED', 'Configure an approved same-candidate reranker', 409)
    rankings, by_id = {}, {}
    preferred = set(policy['source_ids']) if policy['mode'] == 'PREFER' else set()
    try:
        for channel in config.channels:
            group = vector_allowed if channel == 'dense' else allowed
            if not group:
                rankings[channel] = []
                continue
            gateway._unchanged(actor, scope_id, state)
            vector = None
            if channel == 'dense':
                vector = provider.query(query)
                if (not isinstance(vector, (list, tuple)) or len(vector) != provider.dimension or
                        any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                            not math.isfinite(value) for value in vector)):
                    raise PlatformError('INVALID_EMBEDDING', 'Query vector violates the published model contract', 503)
                gateway._unchanged(actor, scope_id, state)
            groups = [[row for row in group if row['source_id'] in preferred],
                      [row for row in group if row['source_id'] not in preferred]] if preferred else [group]
            rows = []
            for selected_group in groups:
                remaining = config.candidate_limit - len(rows)
                if not selected_group or remaining <= 0:
                    continue
                gateway._unchanged(actor, scope_id, state)
                if channel == 'dense':
                    raw = gateway.platform.backend.dense(build['index_name'], actor.tenant_id, scope_id,
                                                         selected_group, vector, remaining)
                else:
                    raw = gateway.platform.backend.search(build['index_name'], actor.tenant_id, scope_id,
                        selected_group, query, remaining,
                        **({'exclude_citation_ids': excluded} if excluded else {}))
                rows.extend(_check_candidates(gateway, actor, scope_id, raw, policy, state, bindings,
                                               selected_group, limit=remaining))
            rankings[channel] = [item for item in rows if item['citation_id'] not in excluded]
            by_id.update((item['citation_id'], item) for item in rankings[channel])
    except SearchUnavailable as exc:
        raise PlatformError('SEARCH_UNAVAILABLE', 'Child retrieval backend unavailable', 503) from exc
    except EmbeddingError as exc:
        raise PlatformError('EMBEDDING_FAILED', 'Configured child embedding failed', 503) from exc
    mapping_snapshot = None
    documents, units = _catalog(gateway, actor, scope_id, state, allowed, mapping=mapping_snapshot)
    if any(key not in units or units[key]['element']['kind'] == 'Page' or
           units[key]['element']['raw_text'] != item['text']
           for key, item in by_id.items()):
        raise PlatformError('INDEX_EVIDENCE_VIOLATION', 'Child does not belong to the published parse', 503)
    scores = {}
    for rows in rankings.values():
        for rank, item in enumerate(rows, 1):
            key = item['citation_id']
            scores[key] = scores.get(key, 0.0) + 1 / (config.rrf_k + rank)
    fused_ids = sorted(scores, key=lambda key: (bool(preferred) and by_id[key]['source_id'] not in preferred,
        -scores[key], key))[:config.fused_limit]
    ranked = [{**by_id[key], 'score': scores[key]} for key in fused_ids]
    fused_trace = [{'citation_id': item['citation_id'], 'score': item['score']} for item in ranked]
    if config.rerank and ranked:
        gateway._unchanged(actor, scope_id, state)


        for item in ranked:
            canonical = gateway._read_one(actor, scope_id, item['citation_id'], policy, state, bindings)
            if (not canonical['policy'].get('rerank', False) or
                    reranker.provider_id not in canonical['policy'].get('allowed_providers', [])):
                raise PlatformError('PROCESSING_NOT_ALLOWED', 'Source does not permit this reranker', 409)
        output = reranker.rank(query, [{key: item[key] for key in EVIDENCE_FIELDS} for item in ranked])
        if (not isinstance(output, list) or len(output) != len(ranked) or
                any(not isinstance(item, dict) or item.get('citation_id') not in fused_ids or
                    isinstance(item.get('score'), bool) or not isinstance(item.get('score'), (int, float)) or
                    not math.isfinite(item['score']) for item in output) or
                len({item['citation_id'] for item in output}) != len(output)):
            raise PlatformError('INVALID_RERANK_OUTPUT', 'Reranker must score exactly the fused candidates', 502)
        values = {item['citation_id']: item['score'] for item in output}
        ranked = sorted([{**item, 'score': values[item['citation_id']]} for item in ranked],
                        key=lambda item: (bool(preferred) and item['source_id'] not in preferred,
                                          -item['score'], fused_ids.index(item['citation_id'])))
    reranked_trace = [{'citation_id': item['citation_id'], 'score': item['score']} for item in ranked]
    delivery_ranked = ranked[:config.top_k]
    selected, expansion = [], []
    admitted = 0
    for item in delivery_ranked:
        if admitted >= config.top_k:
            break
        original_parent_ids = _parents(item, units, config.parent)
        parent_ids = [key for key in original_parent_ids if key not in excluded]
        additional = [key for key in parent_ids if key not in selected]
        if len(selected) + len(additional) > config.max_expanded_units:


            child = item['citation_id']
            kept = child in selected or (child not in excluded and len(selected) < config.max_expanded_units)
            if kept and child not in selected:
                selected.append(child)
            expansion.append({'child_citation_id': child,
                              'status': 'CHILD_ONLY_UNIT_BUDGET' if kept else 'OMITTED_UNIT_BUDGET',
                              'required_units': len(additional),
                              'omitted_citation_ids': [key for key in additional if key not in selected]})
            admitted += int(kept)
            continue
        selected.extend(additional)
        admitted += 1
        expansion.append({'child_citation_id': item['citation_id'],
                          'status': 'COMPLETE' if len(parent_ids) == len(original_parent_ids) else 'EXCLUDED_EXISTING_UNITS',
                          'parent_citation_ids': parent_ids,
                          'excluded_parent_citation_ids': [key for key in original_parent_ids if key in excluded],
                          'original_spans': [{'citation_id': key, 'parse_text_start': units[key]['start'],
                                              'parse_text_end': units[key]['end']} for key in original_parent_ids]})
    title_navigation = None
    if config.title_navigation:
        from .title_packets import title_packet, linked_evidence_packet

        packet, edges = title_packet(query, documents, limit=min(20, config.top_k))
        linked_spans = None
        if not packet and documents:
            linked_spans, links = linked_evidence_packet(query, documents)


            if not links.get('navigation_span_count'):
                linked_spans = []
            source_ids = list(dict.fromkeys(span['source_id'] for span in linked_spans))
            source_lookup = {d['source_id']: d for d in documents}
            packet = [source_lookup[sid] for sid in source_ids]
            edges = links
        title_navigation = {'edges': edges, 'visited_sources': [d['source_id'] for d in packet],
                            'relations_verified': False, 'fallback_to_ranked': not bool(packet),
                            'requested_original_spans': linked_spans}
        for document in packet:


            source_units = sorted((unit for unit in units.values()
                if unit['source']['source_id'] == document['source_id'] and unit['element']['kind'] != 'Page'
                and (linked_spans is None or any(span['source_id'] == document['source_id'] and
                    unit['start'] < span['end'] and unit['end'] > span['start'] for span in linked_spans))),
                key=lambda unit: (unit['start'], unit['end'], unit['citation_id']))
            if not source_units:
                expansion.append({'source_id': document['source_id'], 'status': 'NO_CANONICAL_UNITS'})
                continue
            ids = [unit['citation_id'] for unit in source_units if unit['citation_id'] not in excluded]
            additional = [key for key in ids if key not in selected]
            remaining = config.max_expanded_units - len(selected)
            admitted_ids = additional[:remaining]
            omitted_ids = additional[remaining:]
            selected.extend(admitted_ids)
            expansion.append({'source_id': document['source_id'], 'parent_citation_ids': [key for key in ids if key in selected],
                'omitted_citation_ids': omitted_ids,
                'status': ('PARTIAL_UNIT_BUDGET' if admitted_ids else 'OMITTED_UNIT_BUDGET') if omitted_ids else
                          ('EXCLUDED_EXISTING_UNITS' if len(ids) != len(source_units) else 'COMPLETE')})
    hits = gateway.read(actor, scope_id, selected, policy)
    gateway._unchanged(actor, scope_id, state)
    return {'hits': [dict(item, retrieval_method=PROTOCOL) for item in hits],
        'trace': {'protocol': PROTOCOL, 'config': asdict(config), 'manifest_id': state['manifest_id'],
            'rankings': {channel: [{'citation_id': item['citation_id'], 'score': item['score']} for item in rows]
                         for channel, rows in rankings.items()}, 'fused_ranking': fused_trace,
            'reranked_ranking': reranked_trace,
            'final_candidate_order': [item['citation_id'] for item in ranked],
            'candidate_original_spans': [{'citation_id': item['citation_id'],
                'source_id': item['source_id'], 'source_version_id': item['source_version_id'],
                'parse_snapshot_id': item['parse_snapshot_id'], 'original_sha256': item['original_sha256'],
                'parse_text_start': units[item['citation_id']]['start'],
                'parse_text_end': units[item['citation_id']]['end']} for item in by_id.values()],
            'reranker_manifest': reranker.manifest if config.rerank else None,
            'embedding_manifest': provider.manifest if 'dense' in config.channels else None,
            'excluded_dense_source_count': len({row['source_id'] for row in allowed} -
                                              {row['source_id'] for row in vector_allowed}) if 'dense' in config.channels else 0,
            'expansion': expansion,
            'title_navigation': title_navigation,
            'candidate_unit': 'published_original_child',
            'fusion': {'method': 'rrf', 'k': config.rrf_k},
            'observation_boundary': 'authorized_retrieval_and_parent_delivery; not_final_model_request'}}

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .contracts import AdapterInput, Case, Evidence, Fragment, Plan, Source, Suite, Trace, digest
from .contracts import text_hash
from .runner import run, write_json
from .scoring import corpus_hash


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def migrate(catalog_dir, query_path, plan_path, result_path, output, profile):
    queries = read(query_path)
    legacy_plan = read(plan_path)
    if digest(legacy_plan['plan']) != legacy_plan['sha256']:
        raise ValueError('Legacy plan hash mismatch')
    if digest(queries) != legacy_plan['plan']['queries_sha256']:
        raise ValueError('Archived queries differ from frozen plan')
    results = read(result_path)
    if results['plan_sha256'] != legacy_plan['sha256']:
        raise ValueError('Legacy result plan mismatch')
    rows = results['test'][profile]['scores']
    selected = [q for q in queries['queries'] if q['split'] == 'test']
    if {q['query_id'] for q in selected} != {r['query_id'] for r in rows}:
        raise ValueError('Fixed test denominator mismatch')
    source_map, scopes, manifests, raw_docs, input_hashes = {}, {}, {}, {}, {}
    for path in sorted(catalog_dir.glob('catalog-*.json')):
        catalog = read(path)
        scope = path.stem.removeprefix('catalog-')
        scopes[scope] = []
        manifests[scope] = catalog['manifest_id']
        input_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        for d in catalog['documents']:
            if d['doc_id'] in source_map:
                raise ValueError('Duplicate source across scopes')
            source_map[d['doc_id']] = Source(id=d['doc_id'], kb=scope, version=d['source_version_id'],
                                            text=d['text'], sha256=text_hash(d['text']))
            raw_docs[d['doc_id']] = d
            scopes[scope].append(d['doc_id'])
    recordings = []
    for path in sorted((catalog_dir / 'recordings').glob('*.json')):
        response = read(path)['response']
        if 'hits' in response and response.get('trace', {}).get('rerank'):
            recordings.append((path, response))
    evidence, cases, trace_parts, bindings = [], [], [], []
    for q in selected:
        row = next(r for r in rows if r['query_id'] == q['query_id'])
        request = AdapterInput(case_id=q['query_id'], question=q['query'], kb=q['scope'],
            principal='historical-research-v2-actor', acl_revision='frozen-local-catalog:' + manifests[q['scope']],
            allowed_sources=tuple(scopes[q['scope']]), as_of='2026-10-21', context_budget=4000)
        paths = []
        for group in q['evidence_sets']:
            ids = []
            for docid in group:
                s = source_map[docid]
                eid = q['query_id'] + ':' + docid
                if not any(e.id == eid for e in evidence):
                    evidence.append(Evidence(id=eid, source_id=docid, start=0, end=len(s.text), quote=s.text,
                        fact='Historical document-level evidence label; requires delivery of this entire original.'))
                ids.append(eid)
            if ids:
                paths.append(tuple(ids))
        cases.append(Case(input=request, family=q['family_id'], split='regression', slices=tuple(q['slices']),
                          expected_behavior=q['expected_behavior'], any_of=tuple(paths),
                          qrels={docid: 1 for group in q['evidence_sets'] for docid in group}))
        matches = [(p, r) for p, r in recordings
                   if r['manifest_id'] == manifests[q['scope']]
                   and [d['doc_id'] for d in r['trace']['candidate_ranking']] == row['candidate_ids']
                   and (([d['doc_id'] for d in r['hits']] == row['ranked']
                         and r['trace']['context_used'] == row['context_used'])
                        or (row['abstained'] and not row['ranked'] and row['context_used'] == 0
                            and r['answerable'] is False))]
        for _, response in matches:
            for hit in response['hits']:
                original = raw_docs[hit['doc_id']]
                if any(hit[k] != original[k] for k in ('text', 'source_id', 'source_version_id')):
                    raise ValueError('Recorded hit differs from frozen original')
        if row['status'] == 'SCORED' and not matches:
            raise ValueError('No exact recorded output binding for ' + q['query_id'])
        bindings.append(dict(case_id=q['query_id'], status=row['status'], matching_outputs=len(matches),
            recording_files={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p, _ in matches},
            physical_request_binding='not recovered; recorded output only'))
        fragments = tuple(Fragment(source_id=docid, source_sha256=source_map[docid].sha256,
            version=source_map[docid].version, kb=source_map[docid].kb, start=0,
            end=len(source_map[docid].text), text=source_map[docid].text) for docid in row['candidate_ids'])
        trace_parts.append((q, row, request, fragments))
    suite = Suite(id='research-v2-recorded-' + profile + '-test-document-labels-v1',
                  label_provenance='synthetic_engineering', sources=tuple(source_map.values()),
                  evidence=tuple(evidence), cases=tuple(cases))
    traces = [Trace(case_id=q['query_id'], corpus_hash=corpus_hash(suite),
        implementation='recorded-Luna-ranking:' + profile, principal=request.principal,
        acl_revision=request.acl_revision, status='OK' if row['status']=='SCORED' else 'ERROR',
        error=row['error'], stages={'candidate_pool': fragments, 'retrieval': fragments,
                                   'rerank_top5': fragments[:5], 'rerank_top10': fragments[:10]},
        capture='unobserved') for q, row, request, fragments in trace_parts]
    output.mkdir(parents=True, exist_ok=False)
    for path in (query_path, plan_path, result_path):
        input_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(output / 'migration.json', dict(input_hashes=input_hashes, bindings=bindings,
        label_boundary='Original document AND/OR labels; not semantic or span-annotated gold',
        inference_boundary='Historical output binding only; original physical model requests not recovered',
        new_model_calls=0, training_calls=0))
    run(suite, Plan(suite_hash=digest(suite), mode='replay',
                   required_stages=('candidate_pool', 'retrieval', 'rerank_top5', 'rerank_top10')),
        output / 'history', traces=traces)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Thin migration of research-v2 recorded ranking to the unified evidence contract.\n\nThe original labels are document-level, not semantic answer or span annotations.\nNo old experiment runner is imported or reactivated.\n')
    for name in ('catalog-dir', 'queries', 'legacy-plan', 'results', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--profile', default='tuned_weighted_rrf_rerank')
    args = p.parse_args()
    migrate(args.catalog_dir, args.queries, args.legacy_plan, args.results, args.output, args.profile)

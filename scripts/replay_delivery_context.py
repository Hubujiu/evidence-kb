"""Freeze and score current retention/packing over archived original tool batches.

No HTTP, database, model, embedding or judge is used. Historical authority is an
archived identity, not a newly observed permission grant. All four old questions
and all returned candidates stay in the fixed denominator.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kb_platform.delivery_units import indexed_elements  # noqa: E402
from kb_platform.rag_testing.comparison import compare  # noqa: E402
from kb_platform.rag_testing.context_decisions import CanonicalRetentionAdapter  # noqa: E402
from kb_platform.rag_testing.contracts import Fragment, Plan, Suite, Trace, digest  # noqa: E402
from kb_platform.rag_testing.runner import run, write_json  # noqa: E402
from kb_platform.rag_testing.scoring import corpus_hash  # noqa: E402

BASE = ROOT / 'reports/rag-expansion-20261025'
FILES = ('scripts/replay_delivery_context.py', 'kb_platform/rag_testing/context_decisions.py',
         'kb_platform/agent_context.py', 'kb_platform/evidence_selection.py',
         'kb_platform/evidence_packing.py', 'kb_platform/prose_windows.py',
         'kb_platform/delivery_units.py')
STRATEGIES = {'fifo_query': ('fifo', 'query_blocks'), 'coverage_query': ('coverage', 'query_blocks'),
              'fifo_coverage': ('fifo', 'coverage_blocks'), 'coverage_coverage': ('coverage', 'coverage_blocks')}


def load(path):
    return json.loads(Path(path).read_text('utf-8'))


def checksum(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def code_identity():
    return {name: checksum(ROOT / name) for name in FILES}


def freeze(output):
    original = BASE / 'semantic-active-reentry-results-01'
    mapping_path = BASE / 'semantic-page-http-merged-01/source-and-unit-mapping.json'
    protocol_path = BASE / 'semantic-active-reentry-plan-01/protocol.json'
    suite_path = original / 'seen_and_active/suite.json'
    mapping, protocol = load(mapping_path), load(protocol_path)
    parse_path = ROOT / mapping['identity']['parse_artifact_path']
    if checksum(parse_path) != mapping['identity']['parse_artifact_sha256']:
        raise ValueError('ARCHIVED_PARSE_SHA_MISMATCH')
    document = load(parse_path)['body']['document']
    suite = Suite.model_validate(load(suite_path))
    source = next(s for s in suite.sources if s.id == mapping['identity']['source_id'])
    if '\n'.join(e['raw_text'] for e in document['elements']) != source.text:
        raise ValueError('ARCHIVED_CANONICAL_TEXT_MISMATCH')
    elements = {e['element_id']: e for e in indexed_elements(document, include_pages=True)}
    dependencies = {str(p.relative_to(ROOT)).replace('\\', '/'): checksum(p)
                    for p in (mapping_path, protocol_path, suite_path, parse_path)}
    snapshots = {}
    for case in suite.cases:
        cid = case.input.case_id
        path = original / 'seen_and_active-observations' / (cid + '-component.json')
        observation = load(path)
        dependencies[str(path.relative_to(ROOT)).replace('\\', '/')] = checksum(path)
        template = protocol['cases'][cid]['template']
        catalog, batches = {}, []
        for step in observation['steps']:
            ids = list(step['returned'])
            batch = {'ids': ids, 'offsets': {}}
            for citation in ids:
                unit = mapping['units'][citation]
                element = elements[unit['element_id']]
                text = source.text[unit['start']:unit['end']]
                if text != element['raw_text'] or sha256(text.encode('utf-8')).hexdigest() != unit['text_sha256']:
                    raise ValueError('ARCHIVED_UNIT_RANGE_MISMATCH')
                record = {key: mapping['identity'][key] for key in
                    ('source_id', 'source_version_id', 'parse_snapshot_id', 'build_id')}
                record.update(citation_id=citation, evidence_id=citation, text=text,
                    original_sha256=mapping['identity']['source_version_id'], kind=element['kind'],
                    parse_text_start=unit['start'], parse_text_end=unit['end'],
                    source_text_sha256=source.sha256, anchors=element.get('anchors', []),
                    evidence_ids=unit['evidence_ids'])
                if element.get('table_data') is not None:
                    record['table_data'] = element['table_data']
                fragment = Fragment(source_id=source.id, source_sha256=source.sha256, version=source.version,
                    kb=source.kb, start=unit['start'], end=unit['end'], text=text)
                catalog[citation] = {'record': record, 'fragment': fragment.model_dump(mode='json')}
            if step['name'] == 'read':
                args = protocol['cases'][cid]['tools'][step['index']]['args']
                batch['offsets'] = {key: min(args.get('offset', 0), len(catalog[key]['record']['text']) - 1) for key in ids}
            batches.append(batch)
        state = observation['prepared']['projection']['state']
        snapshots[cid] = {'catalog': catalog, 'batches': batches, 'state': state,
            'system': template['system'], 'history': template['history'], 'question': template['question'],
            'checkpoint': template['checkpoint'], 'selection_query': state.get('work_state', {}).get('resolved_query')}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'suite.json', suite.model_dump(mode='json'))
    write_json(output / 'product-inputs.json', snapshots)
    plan = {'protocol': 'canonical-retention-component/1', 'cases': len(suite.cases),
        'suite_hash': digest(suite), 'product_inputs_hash': digest(snapshots), 'code': code_identity(),
        'upstream_sha256': dependencies, 'strategies': STRATEGIES, 'active_limit': 16, 'input_budget': 118784,
        'window': {'total': 131072, 'output': 4096, 'safety': 8192, 'counting': 'conservative_utf8_estimate'},
        'execution_boundary': 'current_retention_and_context_over_archived_fixed_actions; no_provider_send',
        'authority_boundary': 'archived_source_build_acl; no_new_current_permission_attestation',
        'new_model_embedding_judge_calls': 0, 'gold_in_product_inputs': False,
        'fresh_agent_policy_or_answer': False, 'independent_business_gold': False,
        'metadata_boundary': 'kind/table data reconstructed from SHA-bound original parse and exact unit ranges',
        'comparisons': '2x2 retention and packing; no budget, slots or upstream query changes'}
    write_json(output / 'plan.json', plan)
    return {'plan_sha256': checksum(output / 'plan.json'), 'cases': len(suite.cases), 'new_remote_calls': 0}


def execute(frozen, output, expected_sha):
    if checksum(frozen / 'plan.json') != expected_sha:
        raise ValueError('CONTEXT_PLAN_SHA_MISMATCH')
    plan = load(frozen / 'plan.json')
    suite = Suite.model_validate(load(frozen / 'suite.json'))
    snapshots = load(frozen / 'product-inputs.json')
    if plan['code'] != code_identity() or plan['suite_hash'] != digest(suite) or plan['product_inputs_hash'] != digest(snapshots):
        raise ValueError('FROZEN_CONTEXT_INPUT_CHANGED')
    if any(checksum(ROOT / path) != expected for path, expected in plan['upstream_sha256'].items()):
        raise ValueError('ARCHIVED_CONTEXT_UPSTREAM_CHANGED')
    output.mkdir(parents=True, exist_ok=False)
    reports = {}
    scorer_plan = Plan(suite_hash=digest(suite), mode='replay', required_stages=('retrieval', 'assembly'))
    for name, (retention, packing) in STRATEGIES.items():
        adapter = CanonicalRetentionAdapter(snapshots,
            original_inputs={c.input.case_id: c.input.model_dump(mode='json') for c in suite.cases},
            expected_corpus=corpus_hash(suite), retention=retention, packing=packing,
            active_limit=plan['active_limit'], input_budget=plan['input_budget'])
        traces = []
        for case in suite.cases:
            try:
                traces.append(adapter.run(deepcopy(case.input), tuple(s for s in suite.sources
                    if s.id in case.input.allowed_sources), corpus_hash(suite)))
            except Exception as exc:
                traces.append(Trace(case_id=case.input.case_id, corpus_hash=corpus_hash(suite),
                    implementation=adapter.implementation, principal=case.input.principal,
                    acl_revision=case.input.acl_revision, status='ERROR', error=f'{type(exc).__name__}:{exc}'))
        reports[name] = run(suite, scorer_plan, output / name, traces=traces)
        write_json(output / (name + '-observations.json'), adapter.observations)
    pairs = {name: compare(reports['fifo_query'], report) for name, report in reports.items() if name != 'fifo_query'}
    summary = {'protocol': plan['protocol'], 'plan_sha256': expected_sha,
        'counts': {key: report['counts'] for key, report in reports.items()}, 'paired': pairs,
        'new_model_embedding_judge_calls': 0, 'execution_boundary': plan['execution_boundary'],
        'limitations': ['Four previously seen questions about one public annual report.',
            'Archived queries, sources and upstream planner state, not current Google/Luna planning.',
            'The current deterministic components really execute; replay capture is not a new model payload.',
            'No answer correctness, current live ACL or online latency claim follows from this component replay.']}
    write_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    first = sub.add_parser('freeze')
    first.add_argument('--output', type=Path, required=True)
    second = sub.add_parser('run')
    second.add_argument('--frozen', type=Path, required=True)
    second.add_argument('--output', type=Path, required=True)
    second.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    result = freeze(args.output) if args.command == 'freeze' else execute(args.frozen, args.output, args.plan_sha256)
    print(json.dumps({key: value for key, value in result.items() if key != 'paired'}, ensure_ascii=False))


if __name__ == '__main__':
    main()

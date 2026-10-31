import argparse
import os
from pathlib import Path

from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.search import OpenSearch, backend_dependencies
from .contracts import Plan, Suite, digest
from .default_path_replay import ProductAdapter
from .evidence_experiment import read
from .runner import run, write_json


def main():
    parser = argparse.ArgumentParser(description='Paired real backend execution through current HTTP service and shared evidence scorer.')
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--delivery-window', action='store_true', help='Use the existing deployed delivery context policy')
    parser.add_argument('--search-url', default='http://127.0.0.1:19200')
    parser.add_argument('--postgres', action='store_true', help='Compare embedded baseline with PostgreSQL using KB_DSN')
    args = parser.parse_args()
    suite = Suite.model_validate(read(args.suite))
    policy = None
    if args.delivery_window:
        from kb_platform.delivery import WINDOW
        from kb_platform.agent_context import ContextPolicy
        policy = ContextPolicy(input_budget=WINDOW['input'], output_reserve=WINDOW['output'],
            model_window=WINDOW['total'], safety_reserve=WINDOW['safety'], evidence_strategy='query_blocks')
    budget = policy.input_budget if policy else 24000
    suite = suite.model_copy(update={'id': suite.id + f'/backend-service-wire-{budget}', 'cases': tuple(
        c.model_copy(update={'input': c.input.model_copy(update={'context_budget': budget})}) for c in suite.cases)})
    catalog = [dict(id=s.id, title=s.text.split('\n', 1)[0].lstrip('# ').strip(), text=s.text) for s in suite.sources]
    if args.catalog:
        catalog = read(args.catalog)
    args.output.mkdir(parents=True, exist_ok=False)
    summary, reports = {}, {}
    backends = [('opensearch', OpenSearch(args.search_url)), ('embedded', EmbeddedSearch(args.output / 'indexes'))]
    if args.postgres:
        from kb_platform.postgres_search import PostgresSearch
        backends = [('embedded', EmbeddedSearch(args.output / 'indexes')), ('postgres', PostgresSearch(os.environ['KB_DSN']))]
    for name, backend in backends:
        adapter = ProductAdapter(catalog, True, True, backend=backend, inventory=suite.sources, context_policy=policy)
        adapter.implementation = digest(dict(base=adapter.implementation, backend=name,
            dependencies=backend_dependencies(backend), context_policy=repr(policy)))
        directory = args.output / name
        report = run(suite, Plan(suite_hash=digest(suite), objective='evidence', mode='live',
            required_stages=('retrieval', 'assembly')), directory, adapter=adapter)
        write_json(directory / 'product-traces.json', adapter.products)
        write_json(directory / 'component-observations.json', adapter.observations)
        summary[name] = {k: report[k] for k in ('total', 'counts', 'final_complete_rate')}
        reports[name] = report
        print(name, summary[name], flush=True)
    write_json(args.output / 'summary.json', dict(results=summary, new_remote_calls=0,
        scope='real search + actual HTTP route + captured pre-send messages; scripted model; fixture authority'))
    candidate = {r['case_id']: r for r in reports[backends[1][0]]['rows']}
    losses = []
    for before in reports[backends[0][0]]['rows']:
        after = candidate[before['case_id']]
        for stage, evidence in before.get('stage_evidence', {}).items():
            missing = set(evidence) - set(after.get('stage_evidence', {}).get(stage, []))
            if missing:
                losses.append(dict(case_id=before['case_id'], stage=stage, missing=sorted(missing)))
        if before['outcome'] == 'PASS' and after['outcome'] != 'PASS':
            losses.append(dict(case_id=before['case_id'], lost_pass=after['outcome']))
    write_json(args.output / 'paired-evidence.json', dict(cases=len(suite.cases), losses=losses,
        no_evidence_regression=not losses, budget=budget, criterion='Per-case evidence supersets at every observed stage; no lost PASS'))
    if losses:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

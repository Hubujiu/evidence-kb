"""Current shared rescoring and per-case evidence gates for completed engine probes."""
import argparse
from pathlib import Path

from .backend_probe import summarize
from .backend_probe_data import read, verified_suite
from .contracts import Plan, Trace, digest
from .runner import run, write_json


def evidence_gate(baseline, candidate):
    if baseline['total'] != candidate['total'] or baseline['answerable'] != candidate['answerable']:
        raise ValueError('Different quality denominator')
    losses = {}
    for stage in (k for k in baseline if k.startswith('top')):
        left, right = baseline[stage]['evidence_by_case'], candidate[stage]['evidence_by_case']
        if left.keys() != right.keys():
            raise ValueError('Different paired case set')
        losses[stage] = {qid: sorted(set(values)-set(right[qid])) for qid, values in left.items()
                         if set(values)-set(right[qid])}
    return dict(no_evidence_regression=not any(losses.values()) and not candidate['errors'],
                lost_evidence=losses, component_errors=candidate['errors'],
                boundary='Retrieval evidence only; final request, answer and citation correctness unobserved')


def rescore(runs, output):
    output.mkdir(parents=True, exist_ok=False)
    summaries = {}
    for directory in runs:
        historical = directory/'quality'
        suite = verified_suite(historical)
        plan = Plan.model_validate(read(historical/'plan.json')).model_copy(update={'mode': 'replay'})
        traces = [Trace.model_validate(read(p)) for p in sorted((historical/'traces').glob('*.json'))]
        report = run(suite, plan, output/directory.name, traces=traces)
        summaries[directory.name] = dict(suite_hash=digest(suite), metrics=summarize(suite, report))
    write_json(output/'summary.json', summaries)
    gates = {}
    for baseline, data in summaries.items():
        if baseline not in ('chinese-opensearch-02', 'locomo-exact-02'):
            continue
        for name, candidate in summaries.items():
            if candidate['suite_hash'] == data['suite_hash'] and name != baseline:
                gates[name] = evidence_gate(data['metrics'], candidate['metrics'])
    write_json(output/'quality-gates.json', gates)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rescore(args.runs, args.output)

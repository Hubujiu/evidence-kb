import argparse
import cProfile
import json
from pathlib import Path
import statistics
import subprocess
import time
from uuid import uuid4

import httpx
import numpy as np
import psutil

from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.search import OpenSearch
from .backend_probe import verify_inputs
from .backend_probe_data import read
from .contracts import Suite, digest
from .runner import write_json


class PooledOpenSearch(OpenSearch):
    def __init__(self, url):
        super().__init__(url)
        self.client = httpx.Client(base_url=url, timeout=90, trust_env=False)

    def _request(self, method, path, **kwargs):
        body = kwargs.get('json', {})
        if 'knn' in body.get('query', {}):
            body['query']['knn']['vector']['method_parameters'] = {'ef_search': 128}
        return self.client.request(method, path, **kwargs).raise_for_status().json()


def main():
    parser = argparse.ArgumentParser(description='Interleaved shipping-backend measurements with identical full-document responses.')
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--embedded-indexes', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:19201')
    parser.add_argument('--container', default='kb-backend-shipping-os-20261028')
    parser.add_argument('--reuse-reference', action='store_true')
    args = parser.parse_args()
    verify_inputs(args.inputs)
    args.output.mkdir(parents=True, exist_ok=False)
    suite = Suite.model_validate(read(args.inputs / 'suite.json'))
    queries = read(args.inputs / 'queries.json')
    embedded = EmbeddedSearch(args.embedded_indexes)
    reference = PooledOpenSearch(args.url)
    names, bindings = {}, {}
    write_json(args.output / 'plan.json', dict(input_hash=digest(read(args.inputs / 'input-manifest.json')),
        claim='Warm single-client lexical/dense retrieval latency and full returned units; no end-to-end generation speed claim',
        repeats=5, top_k=20, reference='OpenSearch 2.19.1, pooled HTTP, ef_search=128, heap512m, no replicas',
        candidate='Tantivy + exact normalized f32 vector scan, immutable SQLite metadata',
        measurement='Retrieval including authorization filter, full unit decoding; excludes embedding and scoring',
        new_remote_model_embedding_judge_calls=0))
    for kb in sorted({s.kb for s in suite.sources}):
        local = embedded.index_name('probe', kb, digest(kb)[:32])
        embedded.verify(local)
        units = embedded.read_units(local)
        server = reference.index_name('probe', kb, uuid4().hex)
        if args.reuse_reference:
            matches = reference._request('GET', '/_cat/indices/' + server[:-32] + '*?format=json')
            if len(matches) != 1:
                raise ValueError('REFERENCE_INDEX_AMBIGUOUS')
            server = matches[0]['index']
            observed = reference.read_units(server, [u['unit_id'] for u in units])
            for unit in observed:
                unit.pop('binding_key', None)
            if observed != units:
                raise ValueError('REFERENCE_EXACT_READBACK_MISMATCH')
        else:
            reference.stage(server, units, len(units[0]['vector']))
        names[kb] = dict(embedded=local, opensearch=server)
        bindings[kb] = {u['source_id']: {k: u[k] for k in ('binding_id', 'source_id', 'source_version_id', 'parse_snapshot_id')}
                        for u in units}
    write_json(args.output / 'index-bindings.json', names)
    engines = dict(embedded=embedded, opensearch=reference)
    observations = []

    def execute(name, channel, case):
        request = case.input
        allowed = [bindings[request.kb][s] for s in request.allowed_sources]
        query = queries[request.case_id] if channel == 'dense' else request.question
        started = time.perf_counter()
        method = engines[name].dense if channel == 'dense' else engines[name].search
        hits = method(names[request.kb][name], 'probe', request.kb, allowed, query, 20)
        elapsed = (time.perf_counter() - started) * 1000
        if any(h['source_id'] not in request.allowed_sources or h['kb_id'] != request.kb for h in hits):
            raise PermissionError('Unauthorized backend response')
        return elapsed, [h['unit_id'] for h in hits]

    for name in engines:
        for channel in ('search', 'dense'):
            execute(name, channel, suite.cases[0])
    for repeat in range(5):
        for channel in ('search', 'dense'):
            for case in suite.cases:
                for name in (('opensearch', 'embedded') if repeat % 2 == 0 else ('embedded', 'opensearch')):
                    try:
                        elapsed, ids = execute(name, channel, case)
                        observations.append(dict(repeat=repeat, channel=channel, engine=name,
                            case_id=case.input.case_id, ms=elapsed, ids=ids, error=None))
                    except Exception as exc:
                        observations.append(dict(repeat=repeat, channel=channel, engine=name,
                            case_id=case.input.case_id, ms=None, ids=[], error=str(exc)))
    write_json(args.output / 'observations.json', observations)
    rounds = []
    for repeat in range(5):
        for channel in ('search', 'dense'):
            for name in engines:
                rows = [r for r in observations if (r['repeat'], r['channel'], r['engine']) == (repeat, channel, name)]
                samples = [r['ms'] for r in rows if not r['error']]
                rounds.append(dict(repeat=repeat, channel=channel, engine=name, count=len(rows),
                    errors=sum(bool(r['error']) for r in rows), p50_ms=statistics.median(samples),
                    p95_ms=float(np.percentile(samples, 95))))
    memory = psutil.Process().memory_info()._asdict()
    docker = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', args.container],
                            capture_output=True, text=True, check=True).stdout.strip()
    write_json(args.output / 'results.json', dict(rounds=rounds, process_memory=memory, container=json.loads(docker),
        memory_boundary='Python process includes BOTH clients and corpus/scorer objects; container contains only same 5882 full units. '
                        'OS working set and Windows RSS differ; no full-stack percentage inferred.'))
    profile = cProfile.Profile()
    profile.enable()
    for case in suite.cases:
        execute('embedded', 'dense', case)
    profile.disable()
    profile.dump_stats(str(args.output / 'embedded-dense.prof'))
    print(json.dumps(dict(queries=len(observations), errors=sum(bool(r['error']) for r in observations))))


if __name__ == '__main__':
    main()

from __future__ import annotations

import argparse
import cProfile
from pathlib import Path
import statistics
import time

from .backend_probe import fragments_for, verify_inputs
from .backend_probe_data import read
from .backend_probe_engines import DenseEngine, OpenSearchEngine, TantivyEngine
from .contracts import Suite, digest
from .runner import write_json


def reopen(inputs, directory, rows, vectors):
    manifest = read(directory / 'experiment.json')
    if manifest['input_hash'] != digest(read(inputs / 'input-manifest.json')):
        raise ValueError('Engine/input identity mismatch')
    if read(directory / 'state.json')['status'] != 'COMPLETE':
        raise ValueError('Engine not successfully built')
    name = manifest['name']
    prefix = 'probe-' + digest(str(directory.resolve()))[:16]
    if name.startswith('opensearch'):
        return OpenSearchEngine(rows, prefix, vectors, ef=128 if name.endswith('ef128') else None)
    if name == 'pg_search':
        from .backend_probe_pg import PgSearchEngine
        return PgSearchEngine(rows, prefix.replace('-', '_'), read(inputs/'tokens.json'),
                              read(inputs/'query-tokens.json'))
    if name == 'pgvector':
        from .backend_probe_pg import PgVectorEngine
        return PgVectorEngine(rows, prefix.replace('-', '_'), vectors)
    if name == 'tantivy':
        import tantivy
        engine = TantivyEngine(rows, directory/'index', read(inputs/'tokens.json'), read(inputs/'query-tokens.json'))
        engine.indices = {kb: tantivy.Index.open(str(directory/'index'/digest(kb)[:12]))
                          for kb in {r['kb'] for r in rows}}
        engine.schema = next(iter(engine.indices.values())).schema
        return engine
    engine = DenseEngine(rows, vectors, name, prefix, directory/'index')
    if name == 'usearch':
        from usearch.index import Index
        engine.indices = {kb: Index.restore(str(directory/'index'/(digest(kb)[:12]+'.usearch')))
                          for kb in {r['kb'] for r in rows}}
        for index in engine.indices.values():
            index.expansion_search = 128
    elif name == 'qdrant':
        from qdrant_client import QdrantClient, models
        engine.qm = models
        engine.client = QdrantClient(url='http://127.0.0.1:26333', timeout=90, trust_env=False)
    return engine


def compare_runs(inputs, runs, output):
    import numpy as np
    import psutil
    verify_inputs(inputs)
    output.mkdir(parents=True, exist_ok=False)
    suite = Suite.model_validate(read(inputs/'suite.json'))
    requests = tuple(c.input for c in suite.cases)
    rows = read(inputs/'rows.json')
    vectors = np.load(inputs/'vectors.npy', allow_pickle=False) if (inputs/'vectors.npy').exists() else None
    queries = read(inputs/'queries.json') if vectors is not None else {}
    engines = {p.name: reopen(inputs, p, rows, vectors) for p in runs}
    source_map = {s.id: s for s in suite.sources}
    def search(engine, req):
        started = time.perf_counter()
        ids = engine.search(req, 20, queries.get(req.case_id))
        elapsed = (time.perf_counter()-started)*1000
        fragments_for(ids, rows, req, tuple(source_map[s] for s in req.allowed_sources))
        return ids, elapsed
    protocol = dict(inputs=str(inputs), runs=[str(p) for p in runs], rounds=5, k=20,
        ordering='reverse engine order on odd rounds; same request order',
        boundary='warm component adapter with precomputed embeddings/tokens; not full RAG latency',
        cpu_count=psutil.cpu_count(), initial_cpu_percent=psutil.cpu_percent(interval=0.1),
        memory=psutil.virtual_memory()._asdict(), new_model_calls=0, new_embedding_calls=0, new_judge_calls=0,
        quality_gate='Evidence sets cannot lose any baseline evidence at the same k; counts alone are insufficient',
        acl_challenges='first 12 frozen requests; deterministic 0/1/10/100 percent allowed-source subsets',
        source_timing='Windows client; Docker Linux servers; library and HTTP/SQL paths differ')
    write_json(output/'protocol.json', protocol)
    results = {name: [] for name in engines}
    for repeat in range(5):
        names = list(engines)
        if repeat % 2:
            names.reverse()
        for name in names:
            samples, errors = [], []
            for req in requests:
                try:
                    _, elapsed = search(engines[name], req)
                    samples.append(elapsed)
                except Exception as exc:
                    errors.append(dict(case_id=req.case_id, error=str(exc)))
            results[name].append(dict(round=repeat, samples_ms=samples, errors=errors,
                p50_ms=statistics.median(samples) if samples else None,
                p95_ms=float(np.percentile(samples, 95)) if samples else None))
            write_json(output/'performance.json', results)
    acl = []
    exact = DenseEngine(rows, vectors, 'exact', '', output) if vectors is not None else None
    for req in requests[:12]:
        ordered = sorted(req.allowed_sources, key=lambda sid: digest([req.case_id, sid]))
        for fraction in (0, .01, .1, 1):
            allowed = ordered[:max(1, int(len(ordered)*fraction))] if fraction else []
            challenge = req.model_copy(update={'allowed_sources': tuple(allowed),
                'acl_revision': req.acl_revision + ':probe:' + str(fraction)})
            expected = exact.search(challenge, 20, queries[req.case_id]) if exact else None
            for name, engine in engines.items():
                try:
                    ids, elapsed = search(engine, challenge)
                    overlap = len(set(ids) & set(expected)) / len(expected) if expected else None
                    acl.append(dict(engine=name, case_id=req.case_id, allowed_fraction=fraction,
                        count=len(ids), expected_count=len(expected) if expected is not None else None,
                        exact_neighbor_overlap=overlap, elapsed_ms=elapsed, error=None))
                except Exception as exc:
                    acl.append(dict(engine=name, case_id=req.case_id, allowed_fraction=fraction, error=str(exc)))
    write_json(output/'acl-challenges.json', acl)

    for name, engine in engines.items():
        profiler = cProfile.Profile()
        profiler.enable()
        for req in requests:
            search(engine, req)
        profiler.disable()
        profiler.dump_stats(str(output/(name+'.prof')))
    write_json(output/'state.json', {'status': 'COMPLETE'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Reopen built indexes for interleaved diagnostics and deterministic ACL challenges.')
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--runs', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    compare_runs(args.inputs, args.runs, args.output)

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import statistics
import time

from .backend_probe_data import read, sha, source_rows, verified_suite
from .backend_probe_engines import DenseEngine, OpenSearchEngine, TantivyEngine
from .budget import BudgetLedger
from .cache import StageCache, StageKey
from .contracts import Fragment, Plan, Suite, Trace, digest
from .runner import run, write_json


STAGES = (5, 10, 20, 50, 128)


def freeze_lexical(history, output):
    suite = verified_suite(history)
    rows = source_rows(suite)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'suite.json', suite.model_dump(mode='json'))
    write_json(output / 'rows.json', rows)
    analyzer = OpenSearchEngine(rows, 'analysis-only')
    tokens = {str(r['id']): {'body': analyzer.analyze(r['text'], 'cjk'),
              'exact': analyzer.analyze(r['text'], 'standard'),
              'heading': analyzer.analyze(r['heading'], 'cjk')} for r in rows}
    queries = {c.input.case_id: {'body': analyzer.analyze(c.input.question, 'cjk'),
                                'exact': analyzer.analyze(c.input.question, 'standard')} for c in suite.cases}
    write_json(output / 'tokens.json', tokens)
    write_json(output / 'query-tokens.json', queries)
    write_json(output / 'provenance.json', dict(history=str(history), suite_hash=digest(suite),
        analyzer=analyzer.call('GET', '/')['version'], segmentation='whole source, no gold-based splitting',
        boundary='document-level component comparison, not production child pipeline',
        new_model_calls=0, new_embedding_calls=0, new_judge_calls=0))
    freeze_inputs(output)


def freeze_inputs(directory):
    names = ('suite.json', 'rows.json', 'vectors.npy', 'queries.json', 'tokens.json', 'query-tokens.json')
    write_json(directory / 'input-manifest.json', {name: sha(directory / name)
               for name in names if (directory / name).exists()})


def verify_inputs(directory):
    for name, expected in read(directory / 'input-manifest.json').items():
        target = (directory / name).resolve()
        if not target.is_relative_to(directory.resolve()) or sha(target) != expected:
            raise ValueError('Frozen engine input mismatch')


def fragments_for(ids, rows, request, sources):
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate engine result')
    allowed = {s.id: s for s in sources}
    fragments = []
    for i in ids:
        if type(i) is not int or not 0 <= i < len(rows):
            raise ValueError('Invalid engine row id')
        row = rows[i]
        s = allowed.get(row['source_id'])
        if s is None or s.kb != request.kb or s.id not in request.allowed_sources:
            raise PermissionError('Engine returned unauthorized evidence')
        if (row['source_sha256'] != s.sha256 or row['version'] != s.version or
                s.text[row['start']:row['end']] != row['text']):
            raise ValueError('Engine source binding mismatch')
        fragments.append(Fragment(**{k: row[k] for k in
            ('source_id', 'source_sha256', 'version', 'kb', 'start', 'end', 'text')}))
    return tuple(fragments)


class EngineAdapter:
    def __init__(self, engine, name, rows, queries, output, inputs_hash):
        self.engine, self.rows, self.queries = engine, rows, queries
        self.implementation = 'real-backend-component/1:' + name
        if hasattr(engine, 'backend'):
            from kb_platform.search import backend_dependencies
            self.implementation += ':' + digest(backend_dependencies(engine.backend))
        self.cache = StageCache(output / 'retrieval-cache')
        self.inputs_hash, self.observations = inputs_hash, []

    def run(self, request, sources, corpus):
        key = StageKey(stage='retrieval', implementation=digest({p.name: sha(p) for p in
            Path(__file__).parent.glob('backend_probe*.py')}), upstream=(digest(request), self.inputs_hash),
            config_hash=digest(self.implementation), corpus_hash=corpus, index_snapshot=self.inputs_hash,
            principal=request.principal, acl_revision=request.acl_revision, model_revision='frozen-input',
            sample_id=request.case_id)
        if self.cache.get(key, authorize=lambda: {s.id for s in sources} == set(request.allowed_sources),
                          mode='live', under_test=True) is not None:
            raise ValueError('Under-test retrieval unexpectedly cached')
        vector = self.queries.get(request.case_id)
        started = time.perf_counter()
        ids_by_k = {k: self.engine.search(request, k, vector) for k in STAGES}
        ids = ids_by_k[max(STAGES)]
        elapsed = (time.perf_counter() - started) * 1000
        fragments = fragments_for(ids, self.rows, request, sources)
        stages = {f'top{k}': fragments_for(values, self.rows, request, sources)
                  for k, values in ids_by_k.items()}
        self.cache.put(key, dict(ids_by_k=ids_by_k, elapsed_ms=elapsed))
        self.observations.append(dict(case_id=request.case_id, ids=ids, ids_by_k=ids_by_k, elapsed_ms=elapsed))
        return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
                     principal=request.principal, acl_revision=request.acl_revision, elapsed_ms=elapsed,
                     stages={'retrieval': fragments, **stages},
                     capture='unobserved')


def summarize(suite, report):
    cases = {c.input.case_id: c for c in suite.cases}
    answerable = [c for c in suite.cases if c.any_of]
    result = dict(total=len(cases), answerable=len(answerable), full_acceptance=report['counts'],
                  errors=[r for r in report['rows'] if r['outcome'] in ('ERROR', 'NOT_RUN')])
    for k in STAGES:
        complete, covered_by_case = [], {}
        for row in report['rows']:
            case = cases[row['case_id']]
            evidence = set(row.get('stage_evidence', {}).get(f'top{k}', ()))
            covered_by_case[row['case_id']] = sorted(evidence)
            if case.any_of and any(set(path) <= evidence for path in case.any_of):
                complete.append(row['case_id'])
        result[f'top{k}'] = dict(complete=len(complete), denominator=len(answerable),
                                 complete_cases=complete, evidence_by_case=covered_by_case)
    return result


def execute(inputs, output, name):
    import numpy as np
    import psutil
    verify_inputs(inputs)
    suite = Suite.model_validate(read(inputs / 'suite.json'))
    rows = read(inputs / 'rows.json')
    vectors = np.load(inputs / 'vectors.npy', allow_pickle=False) if (inputs / 'vectors.npy').exists() else None
    queries = read(inputs / 'queries.json') if vectors is not None else {}
    output.mkdir(parents=True, exist_ok=False)
    index_dir = output / 'index'
    index_dir.mkdir()
    prefix = 'probe-' + digest(str(output.resolve()))[:16]
    ledger = BudgetLedger(output / 'ledger.sqlite3', {'remote_calls': 0})
    manifest = dict(name=name, inputs=str(inputs.resolve()), input_hash=digest(read(inputs/'input-manifest.json')),
        source_hashes={p.name: sha(p) for p in Path(__file__).parent.glob('backend_probe*.py')},
        cases=len(suite.cases), rows=len(rows), candidate_k=max(STAGES), repeats=5,
        packages={p: importlib.metadata.version(p) for p in ('numpy', 'tantivy', 'usearch', 'qdrant-client')},
        quality_gate='No baseline evidence lost per case at the same k; zero engine errors and ACL leaks',
        boundary='Real retrieval only; fixed historical/public ACL; no model request or generated answer',
        performance_boundary='single client warm adapter calls including result IDs, excluding embedding and scoring; '
                             'cross-process rounds must be interleaved; not end-to-end RAG latency',
        new_model_calls=0, new_embedding_calls=0, new_judge_calls=0)
    write_json(output / 'experiment.json', manifest)
    before = psutil.Process().memory_info()._asdict()
    if name.startswith('opensearch'):
        engine = OpenSearchEngine(rows, prefix, vectors, ef=128 if name.endswith('ef128') else None)
    elif name == 'tantivy':
        engine = TantivyEngine(rows, index_dir, read(inputs/'tokens.json'), read(inputs/'query-tokens.json'))
    elif name in ('embedded', 'postgres'):
        from .backend_probe_engines import EmbeddedEngine
        if name == 'postgres':
            import os
            from kb_platform.postgres_search import PostgresSearch
            engine = EmbeddedEngine(rows, index_dir, vectors, backend=PostgresSearch(os.environ['KB_DSN']), generation_key=prefix)
        else:
            engine = EmbeddedEngine(rows, index_dir, vectors)
    elif name == 'pg_search':
        from .backend_probe_pg import PgSearchEngine
        engine = PgSearchEngine(rows, prefix.replace('-', '_'), read(inputs/'tokens.json'),
                                read(inputs/'query-tokens.json'))
    elif name == 'pgvector':
        from .backend_probe_pg import PgVectorEngine
        engine = PgVectorEngine(rows, prefix.replace('-', '_'), vectors)
    else:
        engine = DenseEngine(rows, vectors, name, prefix, index_dir,
                             url='http://127.0.0.1:26333')
    started = time.perf_counter()
    engine.build()
    build_ms = (time.perf_counter() - started) * 1000
    after = psutil.Process().memory_info()._asdict()
    adapter = EngineAdapter(engine, name, rows, queries, output, manifest['input_hash'])
    report = run(suite, Plan(suite_hash=digest(suite), mode='live', required_stages=('retrieval',
                 *(f'top{k}' for k in STAGES))), output / 'quality', adapter=adapter)
    summary = summarize(suite, report)
    write_json(output / 'quality-summary.json', summary)
    write_json(output / 'observations.json', adapter.observations)
    performance = dict(build_ms=build_ms, client_memory_before=before, client_memory_after=after,
                       server_memory='measured separately; client RSS is not total stack memory', rounds=[])
    if not summary['errors']:

        for repeat in range(5):
            samples = []
            for case in suite.cases:
                started = time.perf_counter()
                ids = engine.search(case.input, 20, queries.get(case.input.case_id))
                elapsed = (time.perf_counter() - started) * 1000
                fragments_for(ids, rows, case.input,
                              tuple(s for s in suite.sources if s.id in case.input.allowed_sources))
                samples.append(elapsed)
            performance['rounds'].append(dict(repeat=repeat, p50_ms=statistics.median(samples),
                p95_ms=float(np.percentile(samples, 95)), queries=len(samples), errors=0, samples_ms=samples))
    write_json(output / 'performance.json', performance)
    write_json(output / 'budget.json', ledger.summary())
    write_json(output / 'state.json', {'status': 'COMPLETE', 'component_errors': len(summary['errors'])})
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith('top')}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Quality-first component experiment, using the shared runner/scorer and exact inputs.\n\nRetrieval stages are live. Final model request, answer and production ACL are unobserved.\nPerformance is diagnostic until paired quality gates and representative scale are satisfied.\n')
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--engine', choices=('postgres', 'opensearch', 'opensearch-ef128', 'tantivy', 'pg_search',
                                           'exact', 'usearch', 'qdrant', 'pgvector', 'embedded'))
    args = parser.parse_args()
    if args.history:
        freeze_lexical(args.history, args.output)
    else:
        execute(args.inputs, args.output, args.engine)

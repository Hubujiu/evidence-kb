import argparse
from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import subprocess
import time
import traceback

import numpy as np
import psutil

from .backend_probe import verify_inputs
from .backend_probe_data import read
from .backend_probe_engines import EmbeddedEngine
from .contracts import Suite, digest
from .runner import write_json


def worker(pipe, side, inputs, output, dsn):
    os.environ['OPENBLAS_NUM_THREADS'] = '1'
    try:
        suite = Suite.model_validate(read(inputs / 'suite.json'))
        rows = read(inputs / 'rows.json')
        vectors = np.load(inputs / 'vectors.npy', allow_pickle=False)
        queries = read(inputs / 'queries.json')
        if side == 'postgres':
            from kb_platform.postgres_search import PostgresSearch
            backend = PostgresSearch(dsn)
        else:
            from kb_platform.embedded_search import EmbeddedSearch
            backend = EmbeddedSearch(output / 'indexes')
        engine = EmbeddedEngine(rows, output / 'indexes', vectors, backend=backend,
                                generation_key='paired-cost-20261029')
        started = time.perf_counter()
        engine.build()
        build_seconds = time.perf_counter() - started
        count = sum(len(backend.read_units(name)) for name in engine.names.values())
        if count != len(rows):
            raise ValueError('INDEX_READBACK_COUNT_MISMATCH')
        pipe.send({'ready': True, 'pid': os.getpid(), 'build_seconds': build_seconds,
                   'readback_units': count})
        def execute(item):
            channel, case = item
            started = time.perf_counter()
            try:
                ids = engine.search(case.input, 20, queries[case.input.case_id] if channel == 'dense' else None)
                elapsed = (time.perf_counter() - started) * 1000
                allowed = set(case.input.allowed_sources)
                if len(set(ids)) != len(ids) or any(rows[i]['source_id'] not in allowed or
                        rows[i]['kb'] != case.input.kb for i in ids):
                    raise ValueError('BAD_RESULT_OR_SCOPE')
                return dict(case_id=case.input.case_id, ids=ids, ms=elapsed, error=None)
            except Exception as exc:
                return dict(case_id=case.input.case_id, ids=[], ms=None, error=type(exc).__name__)
        while True:
            cmd = pipe.recv()
            if cmd['op'] == 'stop':
                backend.close()
                return
            items = [(cmd['channel'], c) for c in suite.cases]
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=cmd['concurrency']) as pool:
                results = list(pool.map(execute, items))
            pipe.send(dict(seconds=time.perf_counter()-started, results=results))
    except BaseException:
        pipe.send({'fatal': traceback.format_exc()})


def container_stats(name):
    result = subprocess.run(['docker', 'exec', name, 'sh', '-c',
        'cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.stat'],
        check=True, capture_output=True, text=True).stdout.splitlines()
    pairs = [line.split() for line in result]
    values = {p[0]: int(p[1]) for p in pairs if len(p) == 2 and p[1].isdigit()}
    current = next(int(p[0]) for p in pairs if len(p) == 1 and p[0].isdigit())
    return {'cpu_seconds': values['usage_usec']/1_000_000, 'memory_current': current,
            'inactive_file': values.get('inactive_file', 0),
            'working_set': current-values.get('inactive_file', 0)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--old-container', required=True)
    parser.add_argument('--new-container', required=True)
    args = parser.parse_args()
    postgres_dsn = os.environ['KB_DSN']
    verify_inputs(args.inputs)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / 'plan.json', dict(input_hash=digest(read(args.inputs/'input-manifest.json')),
        hypothesis='Compare shipping embedded and PostgreSQL backend on identical frozen units, ACLs and queries',
        repeats=5, channels=['lexical','dense'], concurrency=[1,4], top_k=20,
        boundary='Backend only, no authority DB calls for old backend, no HTTP/model; separate workers and containers',
        warm=True, new_model_embedding_judge_calls=0))
    ctx = mp.get_context('spawn')
    processes, pipes, setup = {}, {}, {}
    containers = {'embedded': args.old_container, 'postgres': args.new_container}
    try:
        for side in ('embedded', 'postgres'):
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=worker, args=(child, side, args.inputs.resolve(),
                (args.output/side).resolve(), postgres_dsn))
            proc.start()
            message = parent.recv()
            if 'fatal' in message:
                raise RuntimeError(message['fatal'])
            processes[side], pipes[side], setup[side] = proc, parent, message
        for side in ('embedded','postgres'):
            for channel in ('lexical','dense'):
                pipes[side].send({'op':'run','channel':channel,'concurrency':1})
                warm = pipes[side].recv()
                if 'fatal' in warm or any(x['error'] for x in warm['results']):
                    raise RuntimeError('WARMUP_FAILED')
        rounds = []
        for concurrency in (1,4):
            for channel in ('lexical','dense'):
                for repeat in range(5):
                    for side in (('embedded','postgres') if repeat % 2 == 0 else ('postgres','embedded')):
                        p = psutil.Process(processes[side].pid)
                        cpu0 = sum(p.cpu_times()[:2])
                        mem0 = p.memory_info().rss
                        db0 = container_stats(containers[side])
                        pipes[side].send({'op':'run','channel':channel,'concurrency':concurrency})
                        result = pipes[side].recv()
                        db1 = container_stats(containers[side])
                        cpu1 = sum(p.cpu_times()[:2])
                        mem1 = p.memory_info().rss
                        if 'fatal' in result:
                            raise RuntimeError(result['fatal'])
                        rows = result['results']
                        errors = sum(bool(r['error']) for r in rows)
                        samples = [r['ms'] for r in rows if not r['error']]
                        rounds.append(dict(side=side,channel=channel,concurrency=concurrency,repeat=repeat,
                            wall_seconds=result['seconds'],completed=len(rows),errors=errors,
                            qps=len(samples)/result['seconds'],p50_ms=statistics.median(samples),
                            p95_ms=float(np.percentile(samples,95)),worker_cpu_seconds=cpu1-cpu0,
                            db_cpu_seconds=db1['cpu_seconds']-db0['cpu_seconds'],worker_rss_before=mem0,
                            worker_rss_after=mem1,db_before=db0,db_after=db1,results=rows))
                        print(side,channel,concurrency,repeat,'errors',errors,flush=True)
                        write_json(args.output/'rounds-progress.json', rounds)
        write_json(args.output/'results.json', dict(setup=setup,rounds=rounds,
            caveats=['DB CPU sampled around worker call, includes stats overhead and background activity',
                     'RSS and DB working set sampled at boundaries, not process peak or physical host total',
                     'Old authority database is idle here; this is not full HTTP service cost']))
    finally:
        for side, proc in processes.items():
            if proc.is_alive():
                pipes[side].send({'op':'stop'})
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.terminate()


if __name__ == '__main__':
    main()

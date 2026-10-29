"""Paired real search API timing on an isolated restored delivery snapshot."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import time
import traceback

import numpy as np
import psutil

from .backend_probe_data import read
from .postgres_cost_benchmark import container_stats
from .runner import write_json


def api_worker(pipe, side, snapshot, identity, queries, dsn):
    try:
        from fastapi.testclient import TestClient
        from kb_platform.api import create_app
        from kb_platform.settings import Settings
        settings = Settings(dsn, '', snapshot / 'application' / 'originals', identity, side)
        app = create_app(settings)
        client = TestClient(app, headers={'Authorization': 'Bearer ' + 'x'*40})
        def execute(item):
            started = time.perf_counter()
            try:
                response = client.post('/knowledge-bases/'+item['kb_id']+'/search',
                    json={'query':item['query'],'channel':'bm25','top_k':5})
                response.raise_for_status()
                body = response.json()
                hits = body['hits']
                if not hits or body['trace']['model_query_executed']:
                    raise ValueError('EMPTY_OR_MODEL_QUERY')
                return dict(case_id=item['case_id'], ms=(time.perf_counter()-started)*1000,
                    ids=[h['citation_id'] for h in hits],
                    texts=[hashlib.sha256(h['text'].encode()).hexdigest() for h in hits],error=None)
            except Exception as exc:
                return dict(case_id=item['case_id'],ms=None,ids=[],texts=[],error=type(exc).__name__)
        pipe.send({'ready': True, 'pid': os.getpid()})
        while True:
            cmd = pipe.recv()
            if cmd['op']=='stop':
                client.close()
                app.state.platform.backend.close()
                return
            start=time.perf_counter()
            with ThreadPoolExecutor(max_workers=cmd['concurrency']) as pool:
                rows=list(pool.map(execute,queries))
            pipe.send({'seconds':time.perf_counter()-start,'results':rows})
    except BaseException:
        pipe.send({'fatal':traceback.format_exc()})


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--old-container',required=True)
    parser.add_argument('--new-container',required=True)
    args=parser.parse_args()
    from kb_platform.search_tokens import tokens
    manifest=read(args.snapshot/'manifest.json')
    queries=[]
    for info in manifest['search_indexes'].values():
        for row in read(args.snapshot/info['file'])['documents']:
            unit=row['source']
            terms=[x for x in tokens(unit['text']) if len(x)>=2]
            if terms:
                queries.append({'case_id':str(len(queries)),'kb_id':unit['kb_id'],'query':' '.join(terms[:3])})
    if len(queries)<60:
        raise ValueError('NOT_ENOUGH_SNAPSHOT_QUERIES')
    queries=queries[:60]
    args.output.mkdir(parents=True,exist_ok=False)
    identity=args.output/'identity.json'
    write_json(identity,{'x'*40:{'tenant_id':manifest['tenant_id'],'principal_id':'local'}})
    write_json(args.output/'plan.json',dict(queries=queries,scope='Real FastAPI search route and PostgreSQL authority; '
        'TestClient in-process transport; frozen actual snapshot 88 units, derived queries, no remote model',
        repeats=5,concurrency=[1,4],top_k=5,new_model_embedding_judge_calls=0))
    containers={'embedded':args.old_container,'postgres':args.new_container}
    dsns={'embedded':os.environ['KB_BENCH_OLD_DSN'],'postgres':os.environ['KB_BENCH_NEW_DSN']}
    ctx=mp.get_context('spawn')
    workers,pipes={},{}
    try:
        for side in ('embedded','postgres'):
            parent,child=ctx.Pipe()
            proc=ctx.Process(target=api_worker,args=(child,side,args.snapshot.resolve(),identity.resolve(),queries,dsns[side]))
            proc.start()
            msg=parent.recv()
            if 'fatal' in msg:
                raise RuntimeError(msg['fatal'])
            workers[side],pipes[side]=proc,parent
        for side in workers:
            pipes[side].send({'op':'run','concurrency':1})
            warm=pipes[side].recv()
            if 'fatal' in warm or any(r['error'] for r in warm['results']):
                raise RuntimeError('WARMUP_FAILED '+str(warm))
        rounds=[]
        for concurrency in (1,4):
            for repeat in range(5):
                for side in (('embedded','postgres') if repeat%2==0 else ('postgres','embedded')):
                    proc=psutil.Process(workers[side].pid)
                    cpu0=sum(proc.cpu_times()[:2])
                    db0=container_stats(containers[side])
                    pipes[side].send({'op':'run','concurrency':concurrency})
                    result=pipes[side].recv()
                    db1=container_stats(containers[side])
                    if 'fatal' in result:
                        raise RuntimeError(result['fatal'])
                    samples=[r['ms'] for r in result['results'] if not r['error']]
                    rounds.append(dict(side=side,concurrency=concurrency,repeat=repeat,
                        wall_seconds=result['seconds'],count=len(result['results']),errors=len(result['results'])-len(samples),
                        p50_ms=statistics.median(samples),p95_ms=float(np.percentile(samples,95)),
                        qps=len(samples)/result['seconds'],worker_cpu_seconds=sum(proc.cpu_times()[:2])-cpu0,
                        db_cpu_seconds=db1['cpu_seconds']-db0['cpu_seconds'],worker_rss=proc.memory_info().rss,
                        db_working_set=db1['working_set'],results=result['results']))
                    write_json(args.output/'rounds-progress.json',rounds)
                    print(side,concurrency,repeat,'errors',rounds[-1]['errors'],flush=True)
        write_json(args.output/'results.json',dict(rounds=rounds,
            limitations=['Same-host TestClient, not network deployment or generation',
                         'Container still includes 5882-unit component database; memory cannot be attributed to 88-unit API corpus']))
    finally:
        for side,proc in workers.items():
            if proc.is_alive():
                pipes[side].send({'op':'stop'})
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.terminate()


if __name__=='__main__':
    main()

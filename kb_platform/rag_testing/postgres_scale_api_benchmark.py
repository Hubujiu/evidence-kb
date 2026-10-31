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

from .backend_probe import verify_inputs
from .backend_probe_data import read
from .contracts import Suite, digest
from .postgres_cost_benchmark import container_stats
from .runner import write_json


def scale_worker(pipe, side, inputs, output, dsn):
    try:
        import psycopg
        from psycopg.types.json import Jsonb
        from fastapi.testclient import TestClient
        from kb_platform.api import create_app
        from kb_platform.application_composition import create_platform
        from kb_platform.settings import Settings

        suite = Suite.model_validate(read(inputs / "suite.json"))
        rows = read(inputs / "rows.json")
        vectors = np.load(inputs / "vectors.npy", allow_pickle=False)
        queries = read(inputs / "queries.json")
        tenant = "cost-scale-20261029"
        identity = output / "identity.json"
        output.mkdir(parents=True, exist_ok=False)
        write_json(identity, {"x" * 40: {"tenant_id": tenant, "principal_id": "reader"}})
        settings = Settings(dsn, "", output / "store", identity, side)


        class FrozenProvider:
            provider_id = "frozen"
            manifest = {"provider": "frozen-vectors", "dimension": int(vectors.shape[1])}

            def query(self, question):
                return by_question[question]

        by_question = {c.input.question: queries[c.input.case_id] for c in suite.cases}
        platform = create_platform(settings, embedding_provider=FrozenProvider())
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (tenant,))
            conn.execute("INSERT INTO tenant_state (tenant_id,revision) VALUES (%s,0)", (tenant,))
        for kb in sorted({r["kb"] for r in rows}):
            group = [r for r in rows if r["kb"] == kb]
            index = platform.backend.index_name(tenant, kb, digest("scale-api-" + kb)[:32])
            units = []
            for r in group:
                units.append(
                    dict(
                        tenant_id=tenant,
                        kb_id=kb,
                        build_id="build",
                        unit_id=str(r["id"]),
                        citation_id=str(r["id"]),
                        binding_id=r["source_id"],
                        source_id=r["source_id"],
                        source_version_id=r["version"],
                        parse_snapshot_id=r["source_sha256"],
                        ordinal=r["id"],
                        kind="Text",
                        text=r["text"],
                        heading_text=r["heading"],
                        evidence_ids=[],
                        anchors=[],
                        vector=vectors[r["id"]].tolist(),
                    )
                )
            platform.backend.stage(index, units, vectors.shape[1])
            if len(platform.backend.read_units(index)) != len(units):
                raise ValueError("INDEX_READBACK_COUNT_MISMATCH")
            with psycopg.connect(dsn) as conn:
                conn.execute(
                    "SELECT set_config('app.tenant_id',%s,true),set_config('app.kb_id',%s,true)", (tenant, kb)
                )
                conn.execute(
                    "INSERT INTO knowledge_bases (tenant_id,kb_id,name,owner_id,manifest_id) VALUES (%s,%s,%s,%s,%s)",
                    (tenant, kb, kb, "reader", "build"),
                )
                conn.execute(
                    "INSERT INTO members VALUES (%s,%s,%s,%s)", (tenant, kb, "reader", Jsonb(["kb.read"]))
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO sources VALUES (%s,%s,%s,%s,%s,0)",
                        [
                            (
                                tenant,
                                r["source_id"],
                                "reader",
                                Jsonb([]),
                                Jsonb({"embedding": True, "allowed_providers": ["frozen"]}),
                            )
                            for r in group
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO source_versions VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                tenant,
                                r["source_id"],
                                r["version"],
                                "fixture.txt",
                                "",
                                r["source_sha256"],
                                "text/plain",
                            )
                            for r in group
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO parse_snapshots VALUES (%s,%s,%s,%s,%s)",
                        [
                            (tenant, r["source_id"], r["version"], r["source_sha256"], Jsonb({}))
                            for r in group
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO bindings VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',0)",
                        [
                            (tenant, kb, r["source_id"], r["source_id"], r["version"], r["source_sha256"])
                            for r in group
                        ],
                    )
                config = {
                    "channels": ["bm25", "dense"],
                    "embedding": platform.embedding_provider.manifest,
                    "vector_source_ids": [r["source_id"] for r in group],
                }
                conn.execute(
                    "INSERT INTO builds VALUES (%s,%s,%s,%s,0,0,%s,%s,'PUBLISHED',%s)",
                    (tenant, kb, "build", None, index, Jsonb(config), len(units)),
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO citations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                tenant,
                                kb,
                                str(r["id"]),
                                "build",
                                r["source_id"],
                                r["source_id"],
                                r["version"],
                                r["source_sha256"],
                                str(r["id"]),
                                r["text"],
                                Jsonb([]),
                                Jsonb([]),
                                None,
                            )
                            for r in group
                        ],
                    )
        app = create_app(settings, platform)
        client = TestClient(app, headers={"Authorization": "Bearer " + "x" * 40})

        def execute(item):
            channel, case = item
            started = time.perf_counter()
            try:
                resp = client.post(
                    "/knowledge-bases/" + case.input.kb + "/search",
                    json={"query": case.input.question, "channel": channel, "top_k": 5},
                )
                resp.raise_for_status()
                body = resp.json()
                hits = body["hits"]
                if body["trace"]["model_query_executed"] != (channel == "dense"):
                    raise ValueError("MODEL_PATH_MISMATCH")
                allowed = set(case.input.allowed_sources)
                if any(h["source_id"] not in allowed for h in hits):
                    raise ValueError("SCOPE_MISMATCH")
                return dict(
                    case_id=case.input.case_id,
                    ms=(time.perf_counter() - started) * 1000,
                    ids=[int(h["citation_id"]) for h in hits],
                    hashes=[hashlib.sha256(h["text"].encode()).hexdigest() for h in hits],
                    error=None,
                )
            except Exception as exc:
                return dict(case_id=case.input.case_id, ms=None, ids=[], hashes=[], error=type(exc).__name__)

        pipe.send({"ready": True, "pid": os.getpid(), "readback_units": len(rows)})
        while True:
            command = pipe.recv()
            if command["op"] == "stop":
                client.close()
                platform.backend.close()
                return
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=command["concurrency"]) as pool:
                results = list(pool.map(execute, [(command["channel"], c) for c in suite.cases]))
            pipe.send({"seconds": time.perf_counter() - start, "results": results})
    except BaseException:
        pipe.send({"fatal": traceback.format_exc()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--old-container", required=True)
    parser.add_argument("--new-container", required=True)
    args = parser.parse_args()
    verify_inputs(args.inputs)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(
        args.output / "plan.json",
        dict(
            input_hash=digest(read(args.inputs / "input-manifest.json")),
            boundary="real FastAPI search route and PostgreSQL ACL/citation authority, TestClient transport; "
            "remote query embeddings replaced by exact frozen vectors; no generation",
            rows=5882,
            cases=60,
            repeats=5,
            concurrency=[1, 4],
            top_k=5,
            new_remote_calls=0,
        ),
    )
    containers = {"embedded": args.old_container, "postgres": args.new_container}
    dsns = {"embedded": os.environ["KB_BENCH_OLD_DSN"], "postgres": os.environ["KB_BENCH_NEW_DSN"]}
    ctx = mp.get_context("spawn")
    workers = {}
    pipes = {}
    setup = {}
    try:
        for side in ("embedded", "postgres"):
            parent, child = ctx.Pipe()
            proc = ctx.Process(
                target=scale_worker,
                args=(child, side, args.inputs.resolve(), (args.output / side).resolve(), dsns[side]),
            )
            proc.start()
            msg = parent.recv()
            if "fatal" in msg:
                raise RuntimeError(msg["fatal"])
            workers[side], pipes[side], setup[side] = proc, parent, msg
        for side in workers:
            for channel in ("bm25", "dense"):
                pipes[side].send({"op": "run", "channel": channel, "concurrency": 1})
                warm = pipes[side].recv()
                if "fatal" in warm or any(r["error"] for r in warm["results"]):
                    raise RuntimeError("WARMUP_FAILED " + str(warm))
        rounds = []
        for concurrency in (1, 4):
            for channel in ("bm25", "dense"):
                for repeat in range(5):
                    for side in ("embedded", "postgres") if repeat % 2 == 0 else ("postgres", "embedded"):
                        p = psutil.Process(workers[side].pid)
                        cpu0 = sum(p.cpu_times()[:2])
                        db0 = container_stats(containers[side])
                        pipes[side].send({"op": "run", "channel": channel, "concurrency": concurrency})
                        result = pipes[side].recv()
                        db1 = container_stats(containers[side])
                        if "fatal" in result:
                            raise RuntimeError(result["fatal"])
                        samples = [x["ms"] for x in result["results"] if not x["error"]]
                        rounds.append(
                            dict(
                                side=side,
                                channel=channel,
                                concurrency=concurrency,
                                repeat=repeat,
                                wall_seconds=result["seconds"],
                                count=len(result["results"]),
                                errors=len(result["results"]) - len(samples),
                                p50_ms=statistics.median(samples),
                                p95_ms=float(np.percentile(samples, 95)),
                                qps=len(samples) / result["seconds"],
                                worker_cpu_seconds=sum(p.cpu_times()[:2]) - cpu0,
                                db_cpu_seconds=db1["cpu_seconds"] - db0["cpu_seconds"],
                                worker_rss=p.memory_info().rss,
                                db_working_set=db1["working_set"],
                                results=result["results"],
                            )
                        )
                        write_json(args.output / "rounds-progress.json", rounds)
                        print(side, channel, concurrency, repeat, "errors", rounds[-1]["errors"], flush=True)
        write_json(args.output / "results.json", dict(setup=setup, rounds=rounds))
    finally:
        for side, proc in workers.items():
            if proc.is_alive():
                pipes[side].send({"op": "stop"})
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.terminate()


if __name__ == "__main__":
    main()

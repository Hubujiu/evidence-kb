"""Offline CLI: freeze validated suites, or rescore immutable traces."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.rag_testing.contracts import Suite, Plan, Trace, digest
from kb_platform.rag_testing.runner import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--suite", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--suite", type=Path, required=True)
    replay.add_argument("--plan", type=Path, required=True)
    replay.add_argument("--traces", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    suite = Suite.model_validate_json(args.suite.read_text("utf-8"))
    if args.command == "freeze":
        plan = Plan(suite_hash=digest(suite))
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(plan.model_dump_json(indent=2))
        print(json.dumps({"suite_hash": digest(suite), "cases": len(suite.cases)}))
        return 0
    plan = Plan.model_validate_json(args.plan.read_text("utf-8"))
    traces = [Trace.model_validate(row) for row in json.loads(args.traces.read_text("utf-8"))]
    report = run(suite, plan, args.output, traces=traces)
    print(json.dumps({k: report[k] for k in ("decision", "total", "counts", "mode")}, ensure_ascii=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[report["decision"]]


if __name__ == "__main__":
    raise SystemExit(main())

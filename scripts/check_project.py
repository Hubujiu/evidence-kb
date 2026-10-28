"""Run local and CI quality gates without provider credentials or live services."""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SHARED = frozenset({"contracts", "cache", "budget", "budget_batches", "provider_capture"})
FOUNDATIONS = frozenset({"contracts", "cache", "budget", "budget_batches"})
EXCLUDED_IMPORTS = frozenset({"output", "outputs", "reports"})


@dataclass(frozen=True)
class CheckResult:
    name: str
    exit_code: int
    log: str


def imports(tree: ast.AST, module: str):
    """Resolve relative imports without importing application code."""
    package = module.rpartition(".")[0]
    dynamic_names = {"__import__", "import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins"}:
            dynamic_names.update(alias.asname or alias.name for alias in node.names
                                 if alias.name in {"import_module", "__import__"})
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            name = "." * node.level + (node.module or "")
            base = importlib.util.resolve_name(name, package) if node.level else name
            yield node.lineno, base
            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}"
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in dynamic_names:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    target = node.args[0].value
                    if target.startswith("."):
                        context = node.args[1] if len(node.args) > 1 else next(
                            (kw.value for kw in node.keywords if kw.arg == "package"), None)
                        if isinstance(context, ast.Constant) and isinstance(context.value, str):
                            target = importlib.util.resolve_name(target, context.value)
                        else:
                            target = "<dynamic-import>"
                    yield node.lineno, target
                else:
                    yield node.lineno, "<dynamic-import>"


def path_mutations(tree: ast.AST):
    names = {"sys.path"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(f"{alias.asname or alias.name}.path" for alias in node.names if alias.name == "sys")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "path")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if ast.unparse(node.func.value) in names and node.func.attr in {
                "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "__setitem__"}:
                yield node.lineno
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else (
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        for target in targets:
            base = target.value if isinstance(target, ast.Subscript) else target
            if ast.unparse(base) in names:
                yield node.lineno


def architecture_violations(root: Path) -> list[str]:
    errors = []
    sources = sorted((root / "kb_platform").rglob("*.py"))
    if not sources:
        return ["No active product sources found"]
    for path in sources:
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts)
        tree = ast.parse(path.read_text("utf-8-sig"), filename=str(relative))
        if path.name.endswith("_contracts.py"):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    errors.append(f"{relative.as_posix()}:{node.lineno}: use unittest assertions in contracts")
        evaluation = module.startswith("kb_platform.rag_testing.")
        foundation = module.split(".")[-1] in FOUNDATIONS and evaluation
        shared = module.split(".")[-1] in SHARED and evaluation
        for line, target in imports(tree, module):
            top = target.split(".")[0]
            reason = None
            if top in EXCLUDED_IMPORTS:
                reason = "generated artifacts cannot be runtime dependencies"
            elif (not evaluation or shared) and top == "scripts":
                reason = "product code cannot import CLI scripts"
            elif (not evaluation or shared) and target.startswith("kb_platform.rag_testing."):
                if target.split(".")[2] not in SHARED:
                    reason = "product code can only use shared RAG contracts, cache, budget, and capture"
            if foundation and not reason and target.startswith("kb_platform."):
                parts = target.split(".")
                if len(parts) >= 3 and parts[1] == "rag_testing" and parts[2] in FOUNDATIONS:
                    continue
                if target != "kb_platform.rag_testing":
                    reason = "RAG foundations cannot depend on product execution or evaluation"
            if target == "<dynamic-import>" and (not evaluation or shared):
                reason = "dynamic runtime imports need a statically reviewable module name"
            if reason:
                errors.append(f"{relative.as_posix()}:{line}: {target}: {reason}")
        if not evaluation or shared:
            for line in path_mutations(tree):
                errors.append(f"{relative.as_posix()}:{line}: product code cannot mutate sys.path")
    if (root / "tests").exists():
        errors.append("Keep contract checks in kb_platform/rag_testing; do not restore a parallel tests directory")
    return sorted(set(errors))


def deny_external_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
                 "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        raise RuntimeError(f"OFFLINE_CHECK_FORBIDS_{event}")


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_worker(kind: str, output: Path) -> int:
    sys.path.insert(0, str(ROOT))
    sys.addaudithook(deny_external_io)
    if kind == "contracts":
        modules = [f"kb_platform.rag_testing.{p.stem}"
                   for p in sorted((ROOT / "kb_platform/rag_testing").glob("*_contracts.py"))]
        if not modules:
            raise RuntimeError("No contract modules discovered")
        suite = unittest.TestSuite()
        counts = {}
        for module in modules:
            tests = unittest.defaultTestLoader.loadTestsFromName(module)
            count = tests.countTestCases()
            if not count:
                raise RuntimeError(f"Contract module has no discoverable tests: {module}")
            counts[module] = count
            suite.addTests(tests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        passed = result.wasSuccessful() and not result.skipped and not result.expectedFailures
        write_json(output / "contracts.json", {
            "decision": "PASS" if passed else "FAIL", "modules": counts, "total": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "scope": "offline component contracts; no production RAG quality claim",
        })
        return 0 if passed else 1
    from kb_platform.rag_testing.contracts import Plan, Suite, Trace
    from kb_platform.rag_testing.runner import run

    config = ROOT / "config/rag-testing"
    suite = Suite.model_validate_json((config / "suite-v1.json").read_text("utf-8"))
    plan = Plan.model_validate_json((config / "plan-v1.json").read_text("utf-8"))
    traces = [Trace.model_validate(row) for row in json.loads((config / "oracle-traces-v1.json").read_text("utf-8"))]
    report = run(suite, plan, output / "scorer-controls", traces=traces)
    print(json.dumps({"decision": report["decision"], "total": report["total"],
                      "counts": report["counts"], "mode": report["mode"],
                      "reused_traces": len(traces), "new_provider_calls": 0}))
    return 0 if report["decision"] == "PASS" else 1


def run_check(name: str, command: list[str], output: Path, env: dict[str, str]) -> CheckResult:
    log = output / f"{name}.log"
    try:
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, timeout=180, check=False)
        code = completed.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{type(exc).__name__}: {exc}\n")
        code = 2
    print(f"{name}: {'PASS' if code == 0 else 'FAIL'} ({log})")
    return CheckResult(name, code, log.name)


def main() -> int:
    if callable(getattr(sys.stdout, "reconfigure", None)):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/checks" / uuid4().hex)
    parser.add_argument("--worker", choices=("contracts", "scorer"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.worker:
        return run_worker(args.worker, output)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("--output must be a new directory; previous results are read-only")
    results = []
    try:
        violations = architecture_violations(ROOT)
    except (SyntaxError, ValueError) as exc:
        violations = [f"{type(exc).__name__}: {exc}"]
    write_json(output / "architecture.json", {"violations": violations})
    results.append(CheckResult("architecture", int(bool(violations)), "architecture.json"))
    print(f"architecture: {'FAIL' if violations else 'PASS'}")
    with tempfile.TemporaryDirectory(prefix="evidence-kb-check-") as scratch:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("KB_", "EVIDENCE_KB_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "AZURE_"))}
        env.update({"KB_DEFER_DEFAULT_APP": "1", "KB_REMOTE_MODELS": "0", "KB_ENABLE_EMBEDDINGS": "0",
                    "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONOPTIMIZE": "0",
                    "KB_STORAGE_ROOT": scratch, "KB_AGENT_ROOT": scratch, "KB_RESEARCH_ROOT": scratch,
                    "KB_IDENTITIES_FILE": str(Path(scratch) / "missing-identities.json")})
        env.pop("PYTHONPATH", None)
        results.append(run_check("lint", [sys.executable, "-m", "ruff", "check", "kb_platform", "scripts"], output, env))
        for kind in ("contracts", "scorer"):
            results.append(run_check(kind, [sys.executable, "-B", str(Path(__file__).resolve()),
                                           "--worker", kind, "--output", str(output)], output, env))
    passed = all(result.exit_code == 0 for result in results)
    write_json(output / "summary.json", {"decision": "PASS" if passed else "FAIL",
        "checks": [asdict(result) for result in results], "new_model_calls": 0,
        "new_embedding_calls": 0, "new_judge_calls": 0,
        "scope": "offline gates; component checks and scorer controls reported separately"})
    print(f"{'PASS' if passed else 'FAIL'}: {output / 'summary.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

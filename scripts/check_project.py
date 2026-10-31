from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import importlib.util
import json
import io
import tokenize
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SHARED = frozenset({"contracts", "cache", "budget", "budget_batches", "provider_capture"})
FOUNDATIONS = frozenset({"contracts", "cache", "budget", "budget_batches"})
EXCLUDED_IMPORTS = frozenset({"output", "outputs", "reports"})
AGENT_DEPENDENCIES = {
    'model_service': frozenset({'agent_engine', 'agent_model', 'model_errors'}),
    'model_plugins': frozenset({'plugin_host', 'model_configuration', 'remote', 'context_budget'}),
    'agent_provider': frozenset(),
    'model_errors': frozenset(),
    'agent_model': frozenset({'agent_provider', 'context_budget', 'agent_schema', 'model_errors'}),
    'agent_engine': frozenset({'agent_native'}),
    'agent_native': frozenset(),
    'agent_decisions': frozenset(),
    'agent_tool_loop': frozenset({'agent_decisions', 'agent_plugins', 'harness_services', 'plugin_host'}),
    'agent_plugins': frozenset({'agent_schema'}),
    'plugin_native': frozenset({'agent_native'}),
    'plugin_host': frozenset({'plugin_native'}),
    'harness_sessions': frozenset({'agent_engine', 'agent_plugins', 'session_identity', 'session_store'}),
    'harness_loop': frozenset({'agent_engine', 'plugin_native', 'plugin_host', 'session_identity'}),
    'harness_services': frozenset({'agent_decisions', 'agent_engine', 'agent_plugins', 'harness_sessions',
                                  'harness_loop', 'plugin_host', 'harness_application', 'harness_context',
                                  'harness_model', 'context_budget', 'session_store'}),
    'context_budget': frozenset(),
    'session_identity': frozenset(),
    'session_store': frozenset({'session_identity'}),
    'plugin_profiles': frozenset({'plugin_native'}),
    'harness_graph': frozenset({'agent_engine', 'plugin_native'}),
    'harness_context': frozenset({'context_budget'}),
    'harness_model': frozenset({'agent_decisions', 'agent_engine', 'agent_model', 'agent_schema', 'context_budget'}),
    'harness_application': frozenset({'harness_sessions'}),
    'harness_profile': frozenset({'harness_application', 'harness_services', 'plugin_host', 'plugin_profiles', 'model_plugins'}),
    'agent_tool_registry': frozenset({'agent_protocol'}),
    'agent_builtin_tools': frozenset({'agent_tools', 'agent_tool_registry', 'agent_history'}),
}
HARNESS_MODULES = frozenset({'agent_decisions', 'agent_engine', 'agent_native', 'agent_tool_loop',
                           'agent_plugins', 'plugin_native', 'plugin_host', 'harness_sessions',
                           'harness_loop', 'harness_services', 'context_budget', 'session_identity', 'session_store',
                           'plugin_profiles', 'harness_graph', 'harness_context', 'harness_model',
                           'harness_application', 'harness_profile'})
NATIVE_DEPENDENCIES = {'agent-kernel': {'agent-harness'}, 'agent-harness': {'serde', 'serde_json'}}


@dataclass(frozen=True)
class CheckResult:
    name: str
    exit_code: int
    log: str


def imports(tree: ast.AST, module: str):

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


def native_architecture_violations(root: Path) -> list[str]:
    errors = []
    workspace = root / 'Cargo.toml'
    if workspace.exists():
        config = tomllib.loads(workspace.read_text('utf-8'))
        if config.get('patch') or config.get('replace'):
            errors.append('Cargo.toml: native dependency substitutions need explicit architecture review')

    def dependencies(table):
        for name, value in table.items():
            if name in {'dependencies', 'dev-dependencies', 'build-dependencies'}:
                yield from value.items()
            elif isinstance(value, dict):
                yield from dependencies(value)

    for crate, allowed in NATIVE_DEPENDENCIES.items():
        directory = root / 'rust' / crate
        manifest = directory / 'Cargo.toml'
        if not manifest.exists():
            continue
        relative = manifest.relative_to(root).as_posix()
        document = tomllib.loads(manifest.read_text('utf-8'))
        if document.get('package', {}).get('build') or (directory / 'build.rs').exists():
            errors.append(f'{relative}: generic native crates cannot run source-generating build scripts')
        for kind in ('lib', 'bin', 'example', 'test', 'bench'):
            targets = document.get(kind, [])
            targets = [targets] if isinstance(targets, dict) else targets
            for target in targets:
                if 'path' in target and not (directory / target['path']).resolve().is_relative_to(directory.resolve()):
                    errors.append(f'{relative}: native build targets must remain inside their crate')
        for name, spec in dependencies(document):
            spec = spec if isinstance(spec, dict) else {'version': spec}
            if name not in allowed or spec.get('package', name) != name:
                errors.append(f'{relative}: {name}: native harness cannot depend on business implementations')
            elif name == 'agent-harness':
                expected = (root / 'rust/agent-harness').resolve()
                if not spec.get('path') or (directory / spec['path']).resolve() != expected:
                    errors.append(f'{relative}: agent-harness must resolve to the reviewed workspace crate')
            elif any(key in spec for key in ('path', 'git', 'workspace')):
                errors.append(f'{relative}: {name}: native foundations must use their declared registry package')
        for source in sorted(directory.rglob('*.rs')):
            text = source.read_text('utf-8')
            patterns = (r'\binclude(?:_str|_bytes)?!\s*\(([^)]*)\)', r'#\s*\[\s*path\s*=\s*([^\]]+)\]')
            for pattern in patterns:
                for match in re.finditer(pattern, text):
                    value = match.group(1).strip()
                    literal = re.fullmatch(r'"([^"\\]+)"\s*,?', value)
                    target = (source.parent / literal.group(1)).resolve() if literal else None
                    if target is None or not target.is_relative_to(directory.resolve()):
                        line = text.count('\n', 0, match.start()) + 1
                        errors.append(f'{source.relative_to(root).as_posix()}:{line}: native source '
                                      'inclusions must remain literal paths inside their crate')
    return errors


def source_quality_violations(root: Path) -> list[str]:
    errors = []
    retired = {'kb_platform.agent_store', 'kb_platform.search_compat'}
    for directory in ('kb_platform', 'scripts'):
        for path in sorted((root / directory).rglob('*.py')):
            relative = path.relative_to(root).as_posix()
            source = path.read_text('utf-8-sig')
            tree = ast.parse(source, filename=relative)
            module = relative.removesuffix('.py').replace('/', '.')
            if module in retired:
                errors.append(f'{relative}: retired compatibility module')
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    errors.append(f'{relative}:{token.start[0]}: code comments are not allowed')
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if ast.get_docstring(node) is not None:
                        errors.append(f'{relative}:{node.body[0].lineno}: docstrings belong in documentation')
            for line, target in imports(tree, module):
                if any(target == name or target.startswith(name + '.') for name in retired):
                    errors.append(f'{relative}:{line}: retired compatibility import: {target}')
    return errors


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
            layer = AGENT_DEPENDENCIES.get(path.stem) if not evaluation else None
            if layer is not None and target.startswith('kb_platform.') and target.split('.')[1] not in layer:
                reason = 'agent boundary cannot depend on this implementation; inject its port at composition'
            if (path.stem in HARNESS_MODULES | {'agent_model', 'agent_provider', 'model_service'}
                    and top in {'openai', 'httpx', 'fastapi', 'requests', 'psycopg'}):
                reason = 'agent contracts and model policy cannot depend on transport or API frameworks'
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
    errors.extend(source_quality_violations(root))
    errors.extend(native_architecture_violations(root))
    return sorted(set(errors))


def deny_external_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
                 "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        raise RuntimeError(f"OFFLINE_CHECK_FORBIDS_{event}")


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def contract_modules():
    return [f"kb_platform.rag_testing.{p.stem}"
            for p in sorted((ROOT / "kb_platform/rag_testing").glob("*_contracts.py"))]


def combine_contract_reports(reports, expected):
    discovered = reports[0].get('discovered', {}) if reports else {}
    selected = [identity for report in reports for identity in report.get('selected', [])]
    modules = {name: sum(owner == name for owner in discovered.values()) for name in expected}
    counts = {key: sum(report.get(key, 0) for report in reports)
              for key in ('total', 'failures', 'errors', 'skipped', 'expected_failures')}
    complete = (bool(expected) and bool(discovered) and set(discovered.values()) == set(expected) and
                len(selected) == len(set(selected)) and set(selected) == set(discovered) and
                all(report.get('discovered') == discovered and
                    report.get('total') == len(report.get('selected', [])) for report in reports))
    passed = complete and all(report.get('decision') == 'PASS' for report in reports) and not any(
        counts[key] for key in ('failures', 'errors', 'skipped', 'expected_failures'))
    return {'decision': 'PASS' if passed else 'FAIL', 'modules': modules, **counts,
            'discovery_complete': complete, 'scope': 'offline component contracts; no production RAG quality claim'}


def run_contract_workers(output):
    def execute(index):
        directory = output / f'contracts-{index}'
        directory.mkdir()
        with tempfile.TemporaryDirectory(prefix=f'evidence-kb-contracts-{index}-') as scratch:
            env = dict(os.environ)
            env.update(KB_STORAGE_ROOT=scratch, KB_AGENT_ROOT=scratch, KB_RESEARCH_ROOT=scratch,
                       KB_IDENTITIES_FILE=str(Path(scratch) / 'missing-identities.json'))
            result = run_check('contracts', [sys.executable, '-B', str(Path(__file__).resolve()),
                '--worker', 'contracts', '--contract-shard', str(index), '--output', str(directory)],
                directory, env, timeout=170)
        try:
            report = json.loads((directory / 'contracts.json').read_text('utf-8'))
        except (OSError, ValueError):
            report = {'decision': 'FAIL', 'modules': {}, 'total': 0, 'errors': 1}
        if result.exit_code:
            report['decision'] = 'FAIL'
        return report

    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(execute, range(4)))
    combined = combine_contract_reports(reports, contract_modules())
    write_json(output / 'contracts.json', combined)
    print(json.dumps(combined))
    return 0 if combined['decision'] == 'PASS' else 1


def run_worker(kind: str, output: Path, shard=None) -> int:
    if kind == 'contracts' and shard is None:
        return run_contract_workers(output)
    sys.path.insert(0, str(ROOT))
    sys.addaudithook(deny_external_io)
    if kind == "contracts":
        modules = contract_modules()
        if not modules:
            raise RuntimeError("No contract modules discovered")
        tests = []
        discovered = {}

        def collect(suite, module):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    collect(test, module)
                else:
                    if test.id() in discovered:
                        raise RuntimeError(f"Duplicate contract test: {test.id()}")
                    discovered[test.id()] = module
                    tests.append(test)

        for module in modules:
            suite = unittest.defaultTestLoader.loadTestsFromName(module)
            if not suite.countTestCases():
                raise RuntimeError(f"Contract module has no discoverable tests: {module}")
            collect(suite, module)
        selected = sorted(tests, key=lambda test: test.id())[shard::4]
        identities = [test.id() for test in selected]
        counts = {module: sum(discovered[identity] == module for identity in identities) for module in modules}
        suite = unittest.TestSuite(selected)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        passed = result.wasSuccessful() and not result.skipped and not result.expectedFailures
        write_json(output / "contracts.json", {
            "decision": "PASS" if passed else "FAIL", "modules": counts, "total": result.testsRun,
            "discovered": discovered, "selected": identities,
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


def run_check(name: str, command: list[str], output: Path, env: dict[str, str], *,
              timeout: int = 180) -> CheckResult:
    log = output / f"{name}.log"
    try:
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, timeout=timeout, check=False)
        code = completed.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{type(exc).__name__}: {exc}\n")
        code = 2
    print(f"{name}: {'PASS' if code == 0 else 'FAIL'} ({log})")
    return CheckResult(name, code, log.name)


def kernel_library_path(target: Path) -> Path:
    name = {"win32": "agent_kernel.dll", "darwin": "libagent_kernel.dylib"}.get(
        sys.platform, "libagent_kernel.so")
    return target / "release" / name


def snapshot_native_library(build: CheckResult, target: Path, output: Path) -> Path | None:

    if build.exit_code:
        return None
    source = kernel_library_path(target)
    destination = output / "native" / source.name
    destination.parent.mkdir()
    shutil.copy2(source, destination)
    return destination


def blocked_contracts(output: Path, reason: str) -> CheckResult:
    log = output / "contracts.log"
    log.write_text(f"BLOCKED: {reason}\n", encoding="utf-8")
    write_json(output / "contracts.json", {
        "decision": "BLOCKED", "reason": reason, "modules": {}, "total": 0,
        "failures": 0, "errors": 1, "skipped": 0, "expected_failures": 0,
        "scope": "component contracts not executed; native build prerequisite failed",
    })
    print(f"contracts: BLOCKED ({log})")
    return CheckResult("contracts", 2, log.name)


def main() -> int:
    if callable(getattr(sys.stdout, "reconfigure", None)):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description='Run local and CI quality gates without provider credentials or live services.')
    parser.add_argument("--output", type=Path, default=ROOT / ".local/checks" / uuid4().hex)
    parser.add_argument("--worker", choices=("contracts", "scorer"), help=argparse.SUPPRESS)
    parser.add_argument('--contract-shard', type=int, choices=range(4), help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.worker:
        return run_worker(args.worker, output, args.contract_shard)
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
                    "CARGO_NET_OFFLINE": "true", "RUSTUP_AUTO_INSTALL": "0",
                    "KB_STORAGE_ROOT": scratch, "KB_AGENT_ROOT": scratch, "KB_RESEARCH_ROOT": scratch,
                    "KB_IDENTITIES_FILE": str(Path(scratch) / "missing-identities.json")})
        env.pop("PYTHONPATH", None)


        target = ROOT / "target/checks" / uuid4().hex
        cargo = ["cargo", "--color", "never"]
        results.append(run_check("rust-format", cargo + ["fmt", "--all", "--", "--check"], output, env))
        native_args = ["--workspace", "--locked", "--offline", "--target-dir", str(target)]
        results.append(run_check("rust-clippy", cargo + ["clippy", *native_args, "--all-targets",
                                                        "--", "-D", "warnings"], output, env, timeout=600))
        build = run_check("rust-build", cargo + ["build", "--release", *native_args],
                          output, env, timeout=600)
        results.append(build)
        native_library = None
        block_reason = "the current Rust release build failed"
        try:
            native_library = snapshot_native_library(build, target, output)
        except OSError as exc:
            block_reason = f"the current Rust release library could not be copied: {exc}"
        results.append(run_check("rust-tests", cargo + ["test", *native_args], output, env, timeout=600))
        if native_library is not None:
            env["KB_AGENT_KERNEL_LIBRARY"] = str(native_library)
        results.append(run_check("lint", [sys.executable, "-m", "ruff", "check", "kb_platform", "scripts"], output, env))
        for kind in ("contracts", "scorer"):
            if kind == "contracts" and native_library is None:
                results.append(blocked_contracts(output, block_reason))
                continue
            results.append(run_check(kind, [sys.executable, "-B", str(Path(__file__).resolve()),
                                           "--worker", kind, "--output", str(output)], output, env))
    passed = all(result.exit_code == 0 for result in results)
    write_json(output / "summary.json", {"decision": "PASS" if passed else "FAIL",
        "checks": [asdict(result) for result in results], "new_model_calls": 0,
        "new_embedding_calls": 0, "new_judge_calls": 0,
        "native_library": str(native_library) if native_library is not None else None,
        "scope": "offline gates; component checks and scorer controls reported separately"})
    print(f"{'PASS' if passed else 'FAIL'}: {output / 'summary.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

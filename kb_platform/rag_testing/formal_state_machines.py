from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = 'formal/registry-v1.json'
LOCK = 'formal/toolchain-v1.json'
STATE_NAMES = frozenset({'state', 'status', 'phase'})
FRAME = re.compile(r'@!@!@STARTMSG (\d+):(\d+) @!@!@\r?\n(.*?)@!@!@ENDMSG \1 @!@!@', re.S)
CFG_SECTIONS = frozenset({'SPECIFICATION', 'INIT', 'NEXT', 'CONSTANT', 'CONSTANTS',
    'INVARIANT', 'INVARIANTS', 'PROPERTY', 'PROPERTIES', 'CONSTRAINT', 'CONSTRAINTS',
    'ACTION_CONSTRAINT', 'ACTION_CONSTRAINTS', 'SYMMETRY', 'VIEW', 'CHECK_DEADLOCK',
    'ALIAS', 'POSTCONDITION'})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Repository-relative nonempty path required')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f'Missing or escaping repository file: {relative}')
    return path


def python_symbols(source: str) -> dict[str, ast.AST]:
    symbols = {'<module>': ast.parse(source)}

    def collect(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = '.'.join(filter(None, (prefix, child.name)))
                symbols[name] = child
                collect(child, name)
            else:
                collect(child, prefix)

    collect(symbols['<module>'], '')
    return symbols


def source_digest(root: Path, binding: dict) -> str:
    path = local_file(root, binding['path'])
    source = path.read_text('utf-8')
    if binding['language'] == 'python':
        symbols = python_symbols(source)
        if binding['symbol'] not in symbols:
            raise ValueError(f'Missing Python symbol: {binding["symbol"]}')
        options = {'show_empty': True} if sys.version_info >= (3, 13) else {}
        return digest(ast.dump(symbols[binding['symbol']], include_attributes=False, **options).encode())
    if binding['language'] != 'rust':
        raise ValueError('Source binding language must be python or rust')
    leaf = binding['symbol'].replace('::', '.').rsplit('.', 1)[-1]
    if leaf != '<module>' and not re.search(r'\b(?:fn|enum|struct)\s+' + re.escape(leaf) + r'\b', source):
        raise ValueError(f'Missing Rust declaration: {binding["symbol"]}')
    return digest(path.read_bytes().replace(b'\r\n', b'\n'))


def state_candidates(root: Path, roots: list[str]) -> set[tuple[str, str]]:
    found = set()
    for directory in roots:
        base = (root / directory).resolve()
        if not base.is_relative_to(root.resolve()) or not base.is_dir():
            raise ValueError(f'Invalid discovery root: {directory}')
        for path in sorted(base.rglob('*.py')):
            if path.name.endswith('_contracts.py') or '__pycache__' in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            symbols = python_symbols(path.read_text('utf-8'))
            for symbol, node in symbols.items():
                if isinstance(node, ast.ClassDef) and re.search(r'(State|Status|Phase)$', node.name):
                    found.add((relative, symbol))
                body = [node]
                while body:
                    item = body.pop()
                    if item is not node and isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    targets = item.targets if isinstance(item, ast.Assign) else (
                        [item.target] if isinstance(item, (ast.AnnAssign, ast.AugAssign)) else [])
                    for target in targets:
                        name = target.attr if isinstance(target, ast.Attribute) else (
                            target.id if isinstance(target, ast.Name) else None)
                        if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                            name = target.slice.value
                        if name in STATE_NAMES:
                            found.add((relative, symbol))
                    body.extend(ast.iter_child_nodes(item))
        for path in sorted(base.rglob('*.rs')):
            if path.name == 'tests.rs' or 'tests' in path.relative_to(base).parts:
                continue
            for name in re.findall(r'\benum\s+([A-Za-z_][A-Za-z_0-9]*)', path.read_text('utf-8')):
                if re.search(r'(State|Status|Phase)$', name):
                    found.add((path.relative_to(root).as_posix(), name))
    return found


def configured_operators(config: str, sections: set[str]) -> set[str]:
    tokens = re.sub(r'\\\*[^\n]*', '', config).split()
    result = set()
    collecting = False
    for token in tokens:
        if token in CFG_SECTIONS:
            collecting = token in sections
        elif collecting:
            result.add(token)
    return result


def operator_names(value, *, allow_empty: bool) -> bool:
    return isinstance(value, list) and (allow_empty or bool(value)) and all(
        isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name)
        for name in value) and len(set(value)) == len(value)


def registry_violations(root: Path, registry: dict | None = None) -> list[str]:
    errors = []
    try:
        registry = registry if registry is not None else json.loads(local_file(root, REGISTRY).read_text('utf-8'))
        if registry.get('schema_version') != 1 or not registry.get('machines'):
            raise ValueError('Registry v1 requires a nonempty machine inventory')
        ids = set()
        bindings = set()
        model_files = {spec[key] for spec in control_specs() for key in ('module', 'config')}
        for machine in registry['machines']:
            identity = machine['id']
            if identity in ids or not re.fullmatch(r'[a-z][a-z0-9_-]+', identity):
                raise ValueError(f'Duplicate or invalid machine id: {identity}')
            ids.add(identity)
            if machine['status'] not in {'partial', 'unmodeled'}:
                raise ValueError(f'{identity}: unknown coverage status')
            for key in ('owners', 'states', 'transitions', 'invariants', 'assumptions', 'exclusions', 'source_bindings'):
                if not isinstance(machine[key], list) or not machine[key]:
                    raise ValueError(f'{identity}: nonempty {key} required')
            for owner in machine['owners']:
                local_file(root, owner)
            for binding in machine['source_bindings']:
                key = (binding['path'], binding['symbol'])
                bindings.add(key)
                actual = source_digest(root, binding)
                if not re.fullmatch('[0-9a-f]{64}', binding['sha256']) or actual != binding['sha256']:
                    errors.append(f'{identity}: source review drift at {key[0]}:{key[1]}')
            models = machine['models']
            controls = machine['negative_controls']
            if (machine['status'] == 'partial') != bool(models):
                errors.append(f'{identity}: partial requires actual models; unmodeled cannot claim models')
            if models and (not isinstance(machine.get('remaining_obligations'), list) or
                           not machine['remaining_obligations'] or not all(
                               isinstance(item, str) and item.strip() for item in machine['remaining_obligations'])):
                errors.append(f'{identity}: partial coverage requires explicit remaining obligations')
            if models and not controls:
                errors.append(f'{identity}: modeled machines require a seeded negative control')
            if any(control.get('kind', 'negative') not in {'negative', 'liveness-negative'} for control in controls):
                errors.append(f'{identity}: negative controls cannot change their expected failure kind')
            for model in models + controls:
                if not re.fullmatch(r'[a-z][a-z0-9_-]+', model['id']):
                    raise ValueError(f'{identity}: invalid model run id')
                module = local_file(root, model['module'])
                config = local_file(root, model['config'])
                if not model['module'].startswith('formal/') or not model['config'].startswith('formal/'):
                    raise ValueError(f'{identity}: model inputs must be under formal/')
                if not re.search(r'\bMODULE\s+' + re.escape(module.stem) + r'\b', module.read_text('utf-8')):
                    errors.append(f'{identity}: module declaration does not match file name')
                model_files.update((model['module'], model['config']))
                if (type(model['timeout_seconds']) is not int or type(model['heap_mb']) is not int or
                        not 1 <= model['timeout_seconds'] <= 120 or not 64 <= model['heap_mb'] <= 512):
                    errors.append(f'{identity}: model exceeds reviewed time/heap limits')
                text = config.read_text('utf-8')
                configured = configured_operators(text, {'INVARIANT', 'INVARIANTS'})
                if model in models:
                    if not operator_names(model['invariants'], allow_empty=False):
                        errors.append(f'{identity}: positive models require a nonempty unique invariant-name list')
                        continue
                    if not operator_names(model['properties'], allow_empty=True):
                        errors.append(f'{identity}: temporal properties require a unique property-name list')
                        continue
                    if not isinstance(model['bounds'], dict) or not model['bounds'] or set(model['invariants']) != configured:
                        errors.append(f'{identity}: explicit finite bounds and exact configured invariants required')
                    properties = configured_operators(text, {'PROPERTY', 'PROPERTIES'})
                    if set(model['properties']) != properties:
                        errors.append(f'{identity}: configured temporal properties differ from registry')
                elif model.get('kind') == 'liveness-negative':
                    if configured_operators(text, {'PROPERTY', 'PROPERTIES'}) != {model['expected_property']}:
                        errors.append(f'{identity}: liveness negative control must isolate its named property')
                elif model['expected_invariant'] not in configured:
                    errors.append(f'{identity}: negative control must target a configured invariant')
        if 'ingestion_migration' in ids:
            from .ingestion_migration_rules import ingestion_migration_violations
            errors.extend(ingestion_migration_violations(root))
        exclusions = registry['discovery']['exclusions']
        excluded = {(row['path'], row['symbol']) for row in exclusions if row.get('reason', '').strip()}
        if len(excluded) != len(exclusions):
            errors.append('Every discovery exclusion requires a unique path/symbol and reason')
        candidates = state_candidates(root, registry['discovery']['roots'])
        for path, symbol in sorted(candidates):
            covered = any(path == owner and (parent == '<module>' or symbol == parent or symbol.startswith(parent + '.'))
                          for owner, parent in bindings)
            if not covered and (path, symbol) not in excluded:
                errors.append(f'Unregistered state candidate: {path}:{symbol}')
        for path, symbol in sorted(excluded - candidates):
            errors.append(f'Stale discovery exclusion: {path}:{symbol}')
        for path in sorted((root / 'formal').rglob('*')):
            if path.suffix in {'.tla', '.cfg'}:
                if path.relative_to(root).as_posix() not in model_files:
                    errors.append(f'Unregistered model input: {path.relative_to(root)}')
    except (KeyError, TypeError, ValueError, OSError, SyntaxError) as exc:
        errors.append(f'Invalid formal registry: {exc}')
    return sorted(set(errors))


def parse_tlc(log: str) -> dict:
    frames = [(int(code), int(severity), body.strip()) for code, severity, body in FRAME.findall(log)]
    remainder = FRAME.sub('', log)
    truncated = '@!@!@STARTMSG' in remainder or '@!@!@ENDMSG' in remainder
    states = None
    for code, _, body in frames:
        if code == 2199:
            match = re.search(r'([\d,]+) states generated, ([\d,]+) distinct states found, ([\d,]+) states left', body)
            if match:
                states = dict(zip(('generated', 'distinct', 'remaining'), (int(value.replace(',', '')) for value in match.groups())))
    return {'frames': [{'code': code, 'severity': severity, 'body': body} for code, severity, body in frames],
            'truncated': truncated, 'states': states}


def tlc_verdict(log: str, returncode: int, expected_invariant: str | None = None,
                expected_property: str | None = None) -> bool:
    parsed = parse_tlc(log)
    frames = parsed['frames']
    codes = {frame['code'] for frame in frames}
    if parsed['truncated'] or not {2185, 2186, 2199} <= codes or not parsed['states']:
        return False
    if parsed['states']['distinct'] < 1:
        return False
    if expected_property is not None:
        return expected_invariant is None and returncode == 13 and {2116, 2192, 2267} <= codes and not any(
            frame['severity'] == 2 or (frame['severity'] == 1 and frame['code'] not in {2116, 2264}) for frame in frames)
    if expected_invariant is None:
        return returncode == 0 and 2193 in codes and parsed['states']['remaining'] == 0 and not any(
            frame['severity'] in {1, 2} for frame in frames)
    violations = [frame for frame in frames if frame['code'] in {2107, 2110}]
    return returncode == 12 and bool(violations) and all(
        re.fullmatch(r'Invariant ' + re.escape(expected_invariant) + r' is violated\.?', frame['body'])
        for frame in violations) and not any(frame['severity'] == 2 or
            (frame['severity'] == 1 and frame['code'] not in {2107, 2110, 2121}) for frame in frames)


def control_specs() -> list[dict]:
    base = {'module': 'formal/controls/GateControl.tla', 'heap_mb': 128, 'timeout_seconds': 30}
    liveness = {'module': 'formal/controls/LivenessControl.tla', 'heap_mb': 128, 'timeout_seconds': 30}
    return [dict(base, id='gate-control-positive', config='formal/controls/GateControl.cfg', kind='positive'),
            dict(base, id='gate-control-seeded-bug', config='formal/controls/GateControlBug.cfg',
                 kind='negative', expected_invariant='NoPrematureRelease'),
            dict(base, id='gate-control-invalid-spec', config='formal/controls/GateControlParseError.cfg',
                 kind='invalid', expected_invariant='NoPrematureRelease'),
            dict(liveness, id='gate-control-liveness-positive', config='formal/controls/LivenessControl.cfg', kind='positive'),
            dict(liveness, id='gate-control-liveness-bug', config='formal/controls/LivenessControlBug.cfg',
                 kind='liveness-negative', expected_property='EventuallyActive')]


def run_model(root: Path, jar: Path, output: Path, spec: dict) -> dict:
    directory = output / spec['id']
    directory.mkdir()
    inputs = directory / 'inputs'
    inputs.mkdir()
    snapshots = {}
    for path in sorted((root / 'formal').rglob('*.tla')):
        target = inputs / path.name
        if target.exists():
            raise ValueError(f'Duplicate TLA module filename: {path.name}')
        shutil.copy2(path, target)
        snapshots[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    config = local_file(root, spec['config'])
    properties = configured_operators(config.read_text('utf-8'), {'PROPERTY', 'PROPERTIES'})
    if spec['kind'] == 'liveness-negative' and properties != {spec['expected_property']}:
        raise ValueError('Liveness negative control must isolate its named temporal property')
    shutil.copy2(config, inputs / config.name)
    snapshots[spec['config']] = digest(config.read_bytes())
    command = ['java', f'-Xmx{spec["heap_mb"]}m', '-XX:MaxMetaspaceSize=128m', '-XX:+UseParallelGC',
               '-XX:+ExitOnOutOfMemoryError', '-cp', str(jar), 'tlc2.TLC', '-tool',
               '-workers', '1', '-seed', '1', '-fp', '0', '-fpmem', '0.25',
               '-metadir', str(directory / 'states'), '-config', config.name,
               Path(spec['module']).stem]
    started = time.monotonic()
    timed_out = False
    with (directory / 'tlc.log').open('w', encoding='utf-8') as stream:
        try:
            result = subprocess.run(command, cwd=inputs, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=spec['timeout_seconds'], check=False)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -1
    log = (directory / 'tlc.log').read_text('utf-8', errors='replace')
    expected = spec.get('expected_invariant') if spec['kind'] != 'positive' else None
    expected_property = spec.get('expected_property')
    accepted = not timed_out and tlc_verdict(log, returncode, expected, expected_property)
    if spec['kind'] == 'positive' and properties:
        accepted = accepted and {2192, 2267} <= {frame['code'] for frame in parse_tlc(log)['frames']}
    if spec['kind'] == 'invalid':
        accepted = not timed_out and returncode in {150, 151, 75} and not tlc_verdict(log, returncode, expected)
    record = {'id': spec['id'], 'kind': spec['kind'], 'decision': 'PASS' if accepted else 'FAIL',
              'returncode': returncode, 'timeout': timed_out, 'seconds': time.monotonic() - started,
              'command': command, 'input_sha256': snapshots, 'expected_invariant': expected,
              'expected_property': expected_property,
              'parsed': parse_tlc(log), 'log': (directory / 'tlc.log').relative_to(output).as_posix()}
    (directory / 'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def run_formal(root: Path, output: Path, jar: Path | None = None) -> int:
    output.mkdir(parents=True, exist_ok=False)
    errors = registry_violations(root)
    report = {'decision': 'FAIL',
              'violations': errors, 'results': [], 'new_provider_calls': 0,
              'scope': 'Finite TLC abstractions plus source review-drift lint; not implementation refinement or all-state-machine proof.'}
    try:
        if errors:
            raise ValueError('Formal registry/source lint failed')
        registry = json.loads((root / REGISTRY).read_text('utf-8'))
        lock = json.loads((root / LOCK).read_text('utf-8'))
        report.update(registry_sha256=digest((root / REGISTRY).read_bytes()),
            toolchain_sha256=digest((root / LOCK).read_bytes()), toolchain=lock,
            coverage={machine['id']: machine['status'] for machine in registry['machines']})
        shutil.copy2(root / REGISTRY, output / 'registry-v1.json')
        shutil.copy2(root / LOCK, output / 'toolchain-v1.json')
        jar = (jar or Path(os.environ.get('TLA_TOOLS_JAR', root / lock['default_jar']))).resolve()
        if not jar.is_file() or digest(jar.read_bytes()) != lock['sha256'] or jar.stat().st_size != lock['size_bytes']:
            raise ValueError('Pinned TLC jar missing or mismatched; explicitly run scripts/install_formal_tools.py')
        version = subprocess.run(['java', '-version'], capture_output=True, text=True, timeout=10, check=False)
        report['java_version'] = version.stdout + version.stderr
        if version.returncode or not re.search(r'version "' + str(lock['java_major']) + r'(?:\.|\")', report['java_version']):
            raise ValueError(f'Java {lock["java_major"]} is required; observed runtime saved in report')
        specs = control_specs()
        for machine in registry['machines']:
            specs.extend(dict(model, kind='positive') for model in machine['models'])
            specs.extend(dict(model, kind=model.get('kind', 'negative')) for model in machine['negative_controls'])
        if len({spec['id'] for spec in specs}) != len(specs):
            raise ValueError('Every model/control run requires a unique id')
        if not any(machine['status'] == 'partial' for machine in registry['machines']):
            raise ValueError('At least one real registered machine model is required; control fixtures are not coverage')
        report['expected_runs'] = {spec['id']: spec['kind'] for spec in specs}
        for spec in specs:
            report['results'].append(run_model(root, jar, output, spec))
        from kb_platform.rag_testing.formal_state_machine_contracts import live_suite
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(live_suite(report))
        (output / 'evidence-contracts.log').write_text(stream.getvalue(), encoding='utf-8')
        report['evidence_contracts'] = {'tests': result.testsRun, 'failures': len(result.failures),
            'errors': len(result.errors), 'skipped': len(result.skipped)}
        report['decision'] = 'PASS' if result.wasSuccessful() and result.testsRun and not result.skipped else 'FAIL'
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report['blocker'] = str(exc)
    (output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'decision': report['decision'], 'summary': str(output / 'summary.json')}))
    return 0 if report['decision'] == 'PASS' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Run bounded, pinned offline TLC models and formal evidence contracts.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jar', type=Path)
    parser.add_argument('--lint-only', action='store_true')
    args = parser.parse_args()
    if args.lint_only:
        args.output.mkdir(parents=True, exist_ok=False)
        violations = registry_violations(ROOT)
        (args.output / 'lint.json').write_text(json.dumps({'violations': violations}, indent=2), encoding='utf-8')
        print(json.dumps({'decision': 'FAIL' if violations else 'PASS', 'violations': violations}))
        return int(bool(violations))
    return run_formal(ROOT, args.output.resolve(), args.jar)


if __name__ == '__main__':
    raise SystemExit(main())

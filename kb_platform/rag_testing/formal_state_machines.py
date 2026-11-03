from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from functools import lru_cache
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

from scripts.formal_provider_lint import provider_circuit_violations
from scripts.physical_settlement_lint import settlement_violations
from scripts.sdk_gateway_lint import violations as sdk_gateway_violations
from scripts.canonical_page_checks import canonical_page_violations
from scripts.physical_ledger_checks import ledger_core_violations, local_ranker_settlement_violations
from scripts.receipt_transition_checks import receipt_transition_violations, sqlite_guard_violations
from scripts.presend_admission_checks import presend_admission_violations
from scripts.semantic_manifest_checks import semantic_manifest_violations
from scripts.knowledge_workflow_checks import knowledge_workflow_violations
from scripts.receipt_database_checks import receipt_database_violations, receipt_database_transition_violations, receipt_send_order_violations
from .formal_inline_boolean import inline_boolean_findings, inline_control_fixtures, finding_signature

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
    if isinstance(source, ast.AST):
        raise TypeError('Python symbol discovery requires source text, not an AST')
    symbols = {'<module>': ast.parse(source)}

    def collect(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = '.'.join(filter(None, (prefix, child.name)))
                symbols[name] = child
                collect(child, name)
            elif not isinstance(child, ast.expr):
                collect(child, prefix)

    collect(symbols['<module>'], '')
    return symbols


def source_digest(root: Path, binding: dict) -> str:
    return _source_digest(root, binding, python_symbols)


def _source_digest(root: Path, binding: dict, parse_symbols) -> str:
    path = local_file(root, binding['path'])
    source = path.read_text('utf-8')
    if binding['language'] == 'python':
        symbols = parse_symbols(source)
        if binding['symbol'] not in symbols:
            raise ValueError(f'Missing Python symbol: {binding["symbol"]}')
        options = {'show_empty': True} if sys.version_info >= (3, 13) else {}
        return digest(ast.dump(symbols[binding['symbol']], include_attributes=False, **options).encode())
    if binding['language'] == 'javascript-module':
        if path.suffix != '.mjs' or binding['symbol'] != '<module>':
            raise ValueError('JavaScript bindings require a complete .mjs module')
        return digest(path.read_bytes().replace(b'\r\n', b'\n'))
    if binding['language'] != 'rust':
        raise ValueError('Source binding language must be python, rust or javascript-module')
    leaf = binding['symbol'].replace('::', '.').rsplit('.', 1)[-1]
    if leaf != '<module>' and not re.search(r'\b(?:fn|enum|struct)\s+' + re.escape(leaf) + r'\b', source):
        raise ValueError(f'Missing Rust declaration: {binding["symbol"]}')
    return digest(path.read_bytes().replace(b'\r\n', b'\n'))


@contextmanager
def _source_digest_session():
    parse_symbols = lru_cache(maxsize=128)(python_symbols)

    def reviewed_digest(root, binding):
        return _source_digest(root, binding, parse_symbols)

    try:
        yield reviewed_digest
    finally:
        parse_symbols.cache_clear()


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
                    if not isinstance(item, ast.expr):
                        body.extend(ast.iter_child_nodes(item))
        for path in sorted(base.rglob('*.rs')):
            if path.name == 'tests.rs' or 'tests' in path.relative_to(base).parts:
                continue
            for name in re.findall(r'\benum\s+([A-Za-z_][A-Za-z_0-9]*)', path.read_text('utf-8')):
                if re.search(r'(State|Status|Phase)$', name):
                    found.add((path.relative_to(root).as_posix(), name))
        for path in sorted(base.rglob('*.mjs')):
            found.add((path.relative_to(root).as_posix(), '<module>'))
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


def configuration_behavior(config: str) -> list[str]:
    tokens = re.sub(r'\\\*[^\n]*', '', config).split()
    result = []
    checking = False
    for token in tokens:
        if token in CFG_SECTIONS:
            checking = token in {'INVARIANT', 'INVARIANTS', 'PROPERTY', 'PROPERTIES'}
        if not checking:
            result.append(token)
    return result


def operator_names(value, *, allow_empty: bool) -> bool:
    return isinstance(value, list) and (allow_empty or bool(value)) and all(
        isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name)
        for name in value) and len(set(value)) == len(value)


def registry_violations(root: Path, registry: dict | None = None) -> list[str]:
    with _source_digest_session() as reviewed_digest:
        return _registry_violations(root, registry, reviewed_digest)


def _registry_violations(root: Path, registry: dict | None, reviewed_digest) -> list[str]:
    from scripts.storage_grant_checks import violations as storage_grant_violations

    errors = []
    try:
        if (root / 'kb_platform/rag_testing/sdk_gateway.py').exists():
            errors.extend(sdk_gateway_violations(root))
        canonical = root / 'kb_platform/semantic_page_ingestion.py'
        if canonical.exists():
            errors.extend(canonical_page_violations(canonical.read_text('utf-8')))
        if (root / 'kb_platform/provider_runtime.py').exists():
            source = local_file(root, 'kb_platform/provider_runtime.py').read_text('utf-8')
            errors.extend(provider_circuit_violations(source))
        if any((root / path).exists() for path in ('kb_platform/rag_testing/provider_capture.py',
                                                   'kb_platform/google_semantic_provider.py')):
            errors.extend(settlement_violations(root))
        for path, check in (('kb_platform/rag_testing/budget.py', ledger_core_violations),
                            ('kb_platform/rag_testing/local_rerank.py', local_ranker_settlement_violations)):
            if (root / path).exists():
                errors.extend(check((root / path).read_text('utf-8')))
        registry = registry if registry is not None else json.loads(local_file(root, REGISTRY).read_text('utf-8'))
        if registry.get('schema_version') != 1 or not registry.get('machines'):
            raise ValueError('Registry v1 requires a nonempty machine inventory')
        semantic_owner = 'kb_platform/semantic_pages_v2.py'
        if (root / semantic_owner).exists() or any(machine.get('id') == 'authority_publication'
                                                 for machine in registry['machines']):
            source = local_file(root, semantic_owner).read_text('utf-8')
            errors.extend('authority_publication: ' + error for error in semantic_manifest_violations(source))
        workflow_owner = 'kb_platform/knowledge_turn.py'
        if (root / workflow_owner).exists() or any(machine.get('id') == 'knowledge_workflow'
                                                 for machine in registry['machines']):
            source = local_file(root, workflow_owner).read_text('utf-8')
            errors.extend('knowledge_workflow: ' + error for error in knowledge_workflow_violations(source))
        ids = set()
        bindings = set()
        model_files = {spec[key] for spec in control_specs() for key in ('module', 'config')}
        for machine in registry['machines']:
            identity = machine['id']
            if identity == 'storage_registry':
                errors.extend(storage_grant_violations(root))
            if identity in ids or not re.fullmatch(r'[a-z][a-z0-9_-]+', identity):
                raise ValueError(f'Duplicate or invalid machine id: {identity}')
            ids.add(identity)
            if identity == 'provider_receipts':
                for path, check in (('kb_platform/provider_runtime.py', receipt_transition_violations),
                                    ('kb_platform/database_providers/sqlite_guard.py', sqlite_guard_violations)):
                    errors.extend(f'{identity}: {error}' for error in check(local_file(root, path).read_text('utf-8')))
                if 'kb_platform/provider_receipt_database.py' in machine['owners']:
                    for path, check in (('kb_platform/provider_receipt_database.py', receipt_database_violations),
                                        ('kb_platform/provider_runtime.py', receipt_database_transition_violations),
                                        ('kb_platform/remote.py', receipt_send_order_violations)):
                        errors.extend(f'{identity}: {error}' for error in check(local_file(root, path).read_text('utf-8')))
            if identity == 'remote_retry_ownership':
                from kb_platform.rag_testing.remote_retry_lint import OWNER, SDK, remote_retry_source_violations
                errors.extend(f'{identity}: {error}' for error in remote_retry_source_violations(
                    local_file(root, OWNER).read_text('utf-8'), local_file(root, SDK).read_text('utf-8')))
            if identity == 'provider_admission':
                source = local_file(root, 'kb_platform/remote.py').read_text('utf-8')
                errors.extend(f'{identity}: {error}' for error in presend_admission_violations(source))
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
                actual = reviewed_digest(root, binding)
                if not re.fullmatch('[0-9a-f]{64}', binding['sha256']) or actual != binding['sha256']:
                    errors.append(f'{identity}: source review drift at {key[0]}:{key[1]}')
            if identity == 'tool_executor':
                from kb_platform.rag_testing.tool_retry_lint import OWNER, CALLER, FIXTURE, tool_retry_source_violations, tool_fixture_source_violations
                errors.extend(f'tool_executor: {error}' for error in tool_retry_source_violations(
                    local_file(root, OWNER).read_text('utf-8'), local_file(root, CALLER).read_text('utf-8')))
                errors.extend(f'tool_executor: {error}' for error in tool_fixture_source_violations(
                    local_file(root, FIXTURE).read_text('utf-8')))
            if identity == 'native_storage_sessions' and machine['models']:
                from kb_platform.rag_testing.native_sessions_lint import native_sessions_violations
                errors.extend(f'native_storage_sessions: {error}' for error in native_sessions_violations(
                    local_file(root, 'rust/storage-service/src/service.rs').read_text('utf-8'),
                    local_file(root, 'formal/native_sessions/NativeSessions.tla').read_text('utf-8')))
            models = machine['models']
            controls = machine['negative_controls']
            limitations = machine.get('known_limitations', [])
            if not all(isinstance(items, list) for items in (models, controls, limitations)):
                raise ValueError(f'{identity}: models, negative controls and known limitations must be lists')
            if identity == 'llm_multiblock' and models:
                from kb_platform.rag_testing.multiblock_lint import multiblock_source_violations
                errors.extend(f'llm_multiblock: {error}' for error in multiblock_source_violations(
                    local_file(root, 'rust/llm-service/src/assembler.rs').read_text('utf-8'),
                    local_file(root, 'formal/llm_multiblock/MultiblockAssembler.tla').read_text('utf-8')))
            if identity == 'llm_stream' and models:
                from kb_platform.rag_testing.llm_stream_lint import stream_source_violations
                errors.extend(f'llm_stream: {error}' for error in stream_source_violations(
                    local_file(root, 'rust/llm-service/src/service.rs').read_text('utf-8'),
                    local_file(root, 'rust/llm-service/src/assembler.rs').read_text('utf-8')))
            if identity == 'llm_registry' and models:
                from kb_platform.rag_testing.llm_registry_lint import registry_source_violations
                errors.extend(f'llm_registry: {error}' for error in registry_source_violations(
                    local_file(root, 'rust/llm-service/src/registry.rs').read_text('utf-8'),
                    local_file(root, 'rust/llm-service/src/executor.rs').read_text('utf-8'),
                    local_file(root, 'kb_platform/model_service.py').read_text('utf-8')))
            if identity == 'host_process' and models:
                from kb_platform.rag_testing.host_process_lint import PATHS, host_source_violations
                errors.extend(f'host_process: {error}' for error in host_source_violations(
                    {path: local_file(root, path).read_text('utf-8') for path in PATHS}))
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
            if any(control.get('known_limitation', False) for control in controls):
                errors.append(f'{identity}: known limitations cannot count as negative controls')
            for limitation in limitations:
                associated = [model for model in models if model['id'] == limitation.get('model_id')]
                if (limitation.get('kind') != 'limitation' or len(associated) != 1 or
                        associated[0]['module'] != limitation.get('module')):
                    errors.append(f'{identity}: known limitation must name a positive model using the same module')
                elif configuration_behavior(local_file(root, limitation['config']).read_text('utf-8')) != (
                        configuration_behavior(local_file(root, associated[0]['config']).read_text('utf-8'))):
                    errors.append(f'{identity}: known limitation must preserve its positive model configuration behavior')
                if not isinstance(limitation.get('purpose'), str) or not limitation['purpose'].strip():
                    errors.append(f'{identity}: known limitation requires an explicit explanation')
                if limitation.get('config') in {model['config'] for model in models + controls}:
                    errors.append(f'{identity}: known limitation requires a separate configuration')
            for model in models + controls + limitations:
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
                elif model in limitations:
                    if (not operator_names([model['expected_invariant']], allow_empty=False) or
                            configured != {model['expected_invariant']} or
                            configured_operators(text, {'PROPERTY', 'PROPERTIES'}) or model.get('expected_property')):
                        errors.append(f'{identity}: known limitation must isolate its named safety invariant')
                elif model.get('kind') == 'liveness-negative':
                    if configured_operators(text, {'PROPERTY', 'PROPERTIES'}) != {model['expected_property']}:
                        errors.append(f'{identity}: liveness negative control must isolate its named property')
                elif model['expected_invariant'] not in configured:
                    errors.append(f'{identity}: negative control must target a configured invariant')
        from scripts.postflight_model_checks import postflight_model_violations

        errors.extend(postflight_model_violations(root, registry))
        if {'acceptance_binding_publication', 'acceptance_client_execution'} & ids:
            from .acceptance_output_rules import acceptance_output_violations
            errors.extend(acceptance_output_violations(root))
        if 'acceptance_workflow' in ids:
            from .acceptance_workflow_rules import acceptance_workflow_violations
            errors.extend(acceptance_workflow_violations(root))
        if 'ingestion_migration' in ids:
            from .ingestion_migration_rules import ingestion_migration_violations
            errors.extend(ingestion_migration_violations(root))
        if 'provider_admission' in ids:
            from .provider_admission_rules import provider_admission_violations

            errors.extend(provider_admission_violations(root))
        from scripts.session_catalog_checks import session_catalog_violations

        errors.extend(session_catalog_violations(root, registry))
        from scripts.durable_session_checks import durable_session_violations

        errors.extend(durable_session_violations(root, registry))
        if 'page_recovery' in ids:
            from .page_recovery_rules import page_recovery_violations
            errors.extend(page_recovery_violations(root))
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
                    errors.append(f'Unregistered model input: {path.relative_to(root).as_posix()}')
        from scripts.error_projection_model_checks import error_projection_model_violations

        errors.extend(error_projection_model_violations(root, registry))
        from scripts.rpc_transfer_owner_checks import rpc_transfer_owner_violations

        errors.extend(rpc_transfer_owner_violations(root, registry))
        receipt_owner = root / 'kb_platform/knowledge_contracts.py'
        if receipt_owner.is_file():
            from scripts.check_project import (
                model_capacity_outcome_violations, model_error_projection_violations,
                model_receipt_cleanup_violations, model_receipt_violations,
            )

            tree = ast.parse(receipt_owner.read_text('utf-8'))
            errors.extend(f'model receipts: {error}' for check in
                          (model_receipt_violations, model_capacity_outcome_violations,
                           model_receipt_cleanup_violations, model_error_projection_violations) for error in check(tree))
        if any(model['module'].startswith('formal/plugin_ownership/') for machine in registry.get('machines', [])
               for model in machine.get('models', [])):
            from .formal_plugin_ownership import ownership_violations
            errors.extend(ownership_violations(root))
        if any(machine['id'] == 'python_host_lifecycles' for machine in registry.get('machines', [])):
            from .formal_python_host import ownership_violations
            errors.extend(ownership_violations(root))
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
    if spec['kind'] not in {'positive', 'negative', 'liveness-negative', 'invalid', 'limitation'}:
        raise ValueError('Unknown formal run kind')
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
    if spec['kind'] == 'limitation' and (properties or spec.get('expected_property') or
            configured_operators(config.read_text('utf-8'), {'INVARIANT', 'INVARIANTS'}) != {spec['expected_invariant']}):
        raise ValueError('Known limitation must isolate its named safety invariant')
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
    decision = ('OBSERVED_LIMITATION' if spec['kind'] == 'limitation' else 'PASS') if accepted else 'FAIL'
    record = {'id': spec['id'], 'kind': spec['kind'], 'decision': decision,
              'returncode': returncode, 'timeout': timed_out, 'seconds': time.monotonic() - started,
              'command': command, 'input_sha256': snapshots, 'expected_invariant': expected,
              'expected_property': expected_property,
              'parsed': parse_tlc(log), 'log': (directory / 'tlc.log').relative_to(output).as_posix()}
    if spec['kind'] == 'limitation':
        record.update(model_id=spec['model_id'], purpose=spec['purpose'])
    (directory / 'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def run_inline_export(jar: Path, output: Path, inputs: Path, identity: str, module: str,
                      snapshots: dict, expected: list[tuple]) -> dict:
    directory = output / identity
    directory.mkdir()
    scratch = directory / 'tmp'
    scratch.mkdir()
    command = ['java', '-Xmx256m', '-XX:MaxMetaspaceSize=128m', '-XX:+UseParallelGC',
               '-XX:+ExitOnOutOfMemoryError', f'-Djava.io.tmpdir={scratch}', '-cp', str(jar),
               'tla2sany.xml.XMLExporter', '-o', str(inputs / f'{module}.tla')]
    record = {'id': identity, 'module': module, 'decision': 'ERROR', 'timeout': False,
              'returncode': None, 'command': command, 'input_sha256': snapshots,
              'expected_findings': [list(item) for item in expected], 'findings': None,
              'xml': (directory / 'module.xml').relative_to(output).as_posix(),
              'log': (directory / 'sany.log').relative_to(output).as_posix(),
              'timeout_seconds': 30, 'heap_mb': 256}
    started = time.monotonic()
    try:
        with (directory / 'module.xml').open('wb') as xml, (directory / 'sany.log').open('wb') as log:
            result = subprocess.run(command, cwd=inputs, stdout=xml, stderr=log, timeout=30, check=False)
        record['returncode'] = result.returncode
        if result.returncode:
            raise ValueError('Official SANY XML export failed; see preserved stderr')
        xml = (directory / 'module.xml').read_bytes()
        record['xml_sha256'] = digest(xml)
        record['findings'] = inline_boolean_findings(xml, module)
        actual = finding_signature(record['findings'])
        record['decision'] = 'PASS' if actual == expected else 'FAIL'
        if actual != expected:
            record['blocker'] = 'Inline Boolean assignment review did not match the required findings'
    except subprocess.TimeoutExpired:
        record.update(timeout=True, blocker='Official SANY XML export timed out')
    except (OSError, ValueError) as exc:
        record['blocker'] = str(exc)
    if (directory / 'module.xml').is_file():
        record['xml_sha256'] = digest((directory / 'module.xml').read_bytes())
    record['seconds'] = time.monotonic() - started
    (directory / 'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def run_inline_review(root: Path, jar: Path, output: Path, specs: list[dict]) -> dict:
    output.mkdir()
    inputs = output / 'inputs'
    inputs.mkdir()
    snapshots = {}
    for path in sorted((root / 'formal').rglob('*.tla')):
        target = inputs / path.name
        if target.exists():
            raise ValueError(f'Duplicate TLA module filename: {path.name}')
        shutil.copy2(path, target)
        snapshots[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    modules = sorted({spec['module'] for spec in specs})
    fixtures = inline_control_fixtures(root)
    report = {'decision': 'FAIL', 'expected_modules': modules,
              'expected_controls': [fixture['id'] for fixture in fixtures], 'modules': [], 'controls': [],
              'scope': 'Direct same-line conjunction review from official SANY XML; '
                       'not intent inference, branch coverage or implementation refinement.'}
    for module in modules:
        identity = 'model-' + Path(module).stem
        row = run_inline_export(jar, output, inputs, identity, Path(module).stem, snapshots, [])
        row['source_path'] = module
        report['modules'].append(row)
    for fixture in fixtures:
        identity = 'control-' + fixture['id']
        control_inputs = output / (identity + '-inputs')
        control_inputs.mkdir()
        (control_inputs / f'{fixture["module"]}.tla').write_bytes(fixture['source'])
        row = run_inline_export(jar, output, control_inputs, identity, fixture['module'],
            {fixture['source_path']: fixture['source_sha256']}, fixture['expected'])
        row['fixture_id'] = fixture['id']
        row['fixture_xml_sha256'] = fixture['xml_sha256']
        report['controls'].append(row)
    rows = report['modules'] + report['controls']
    report['decision'] = 'PASS' if rows and all(row['decision'] == 'PASS' for row in rows) else 'FAIL'
    (output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def run_formal(root: Path, output: Path, jar: Path | None = None) -> int:
    output.mkdir(parents=True, exist_ok=False)
    errors = registry_violations(root)
    report = {'decision': 'FAIL',
              'violations': errors, 'results': [], 'known_limitations': [], 'new_provider_calls': 0,
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
        limitations = []
        for machine in registry['machines']:
            specs.extend(dict(model, kind='positive') for model in machine['models'])
            specs.extend(dict(model, kind=model.get('kind', 'negative')) for model in machine['negative_controls'])
            limitations.extend(dict(model, kind='limitation') for model in machine.get('known_limitations', []))
        if len({spec['id'] for spec in specs + limitations}) != len(specs) + len(limitations):
            raise ValueError('Every model/control run requires a unique id')
        if not any(machine['status'] == 'partial' for machine in registry['machines']):
            raise ValueError('At least one real registered machine model is required; control fixtures are not coverage')
        report['expected_runs'] = {spec['id']: spec['kind'] for spec in specs}
        report['expected_limitations'] = {spec['id']: {
            key: spec[key] for key in ('model_id', 'expected_invariant', 'purpose')} for spec in limitations}
        report['registered_model_count'] = sum(len(machine['models']) for machine in registry['machines'])
        report['inline_boolean_review'] = run_inline_review(root, jar, output / 'inline-boolean-review', specs)
        if report['inline_boolean_review']['decision'] != 'PASS':
            raise ValueError('Official SANY inline Boolean assignment review failed; TLC is not claimed as executed')
        for spec in specs:
            report['results'].append(run_model(root, jar, output, spec))
        for spec in limitations:
            report['known_limitations'].append(run_model(root, jar, output, spec))
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

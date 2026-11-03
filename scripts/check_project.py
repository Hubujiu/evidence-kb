from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import faulthandler
import importlib.util
import hashlib
import json
import io
import tokenize
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SHARED = frozenset({"contracts", "cache", "budget", "budget_batches", "provider_capture"})
FOUNDATIONS = frozenset({"contracts", "cache", "budget", "budget_batches"})
DATABASE_FOUNDATIONS = frozenset({'database_contracts', 'database_service', 'database_migrations', 'storage_sdk',
                                 'database_providers', 'persistence_schema', 'persistence_local'})
EXCLUDED_IMPORTS = frozenset({"output", "outputs", "reports"})
AGENT_DEPENDENCIES = {
    'contract_transfer': frozenset(),
    'foundation_services': frozenset({'application_storage', 'llm_contract', 'model_adapters', 'model_clients', 'model_configuration', 'model_service', 'plugin_configuration', 'plugin_contracts', 'remote'}),
    'knowledge_model_service': frozenset({'agent_model', 'context_budget', 'embeddings', 'knowledge_model_ports', 'llm_contract', 'model_errors', 'model_sdk', 'ocr_contract', 'plugin_contracts'}),
    'knowledge_storage_configuration': frozenset({'knowledge_schema', 'persistence_schema', 'session_storage', 'storage_sdk'}),
    'llm_contract': frozenset({'agent_model', 'agent_provider', 'contract_transfer', 'model_errors', 'model_sdk', 'plugin_contracts'}),
    'model_clients': frozenset(),
    'agent_provider': frozenset(),
    'model_errors': frozenset(),
    'protocol_validation': frozenset(),
    'model_sdk': frozenset({'model_errors', 'protocol_validation'}),
    'model_completion': frozenset({'agent_provider', 'model_sdk', 'protocol_validation'}),
    'llm_protocol': frozenset({'model_errors', 'plugin_native'}),
    'storage_protocol': frozenset({'plugin_native', 'database_contracts', 'storage_sdk'}),
    'authentication': frozenset({'session_identity', 'storage_sdk', 'protocol_validation'}),
    'authentication_plugins': frozenset({'authentication', 'plugin_configuration', 'plugin_host',
                                         'storage_plugins', 'storage_sdk'}),
    'agent_model': frozenset({'agent_provider', 'context_budget', 'agent_schema', 'model_errors'}),
    'agent_engine': frozenset({'agent_native'}),
    'agent_native': frozenset(),
    'agent_decisions': frozenset(),
    'agent_tool_loop': frozenset({'agent_decisions', 'agent_plugins', 'harness_services', 'plugin_host'}),
    'agent_plugins': frozenset({'agent_schema'}),
    'plugin_native': frozenset({'agent_native'}),
    'host_protocol': frozenset(),
    'host_worker': frozenset({'host_protocol', 'plugin_native'}),
    'plugin_host': frozenset({'plugin_native', 'storage_sdk', 'protocol_validation', 'plugin_configuration'}),
    'harness_sessions': frozenset({'agent_engine', 'agent_plugins', 'session_identity', 'session_store'}),
    'harness_loop': frozenset({'agent_engine', 'plugin_native', 'plugin_host', 'session_identity'}),
    'harness_services': frozenset({'agent_decisions', 'agent_engine', 'agent_plugins', 'harness_sessions',
                                  'harness_loop', 'plugin_host', 'harness_application', 'harness_context',
                                  'harness_model', 'context_budget', 'session_store', 'plugin_configuration',
                                  'storage_plugins', 'storage_sdk'}),
    'plugin_configuration': frozenset({'context_budget'}),
    'context_budget': frozenset(),
    'session_identity': frozenset(),
    'session_store': frozenset({'session_identity', 'session_storage', 'database_contracts', 'persistence_local'}),
    'database_contracts': frozenset(),
    'database_migrations': frozenset({'database_contracts'}),
    'database_service': frozenset({'database_contracts', 'database_migrations', 'database_process'}),
    'database_process': frozenset({'database_contracts'}),
    'storage_sdk': frozenset({'database_contracts', 'protocol_validation'}),
    'storage_service': frozenset({'storage_sdk', 'storage_process', 'storage_protocol', 'database_contracts', 'implementation_dependencies',
                                 'protocol_validation'}),
    'storage_plugins': frozenset({'storage_sdk', 'storage_service', 'plugin_configuration', 'plugin_host',
                                 'protocol_validation'}),
    'storage_process': frozenset({'storage_sdk', 'blob_service', 'database_contracts', 'storage_protocol'}),
    'storage_cache_plugins': frozenset({'storage_sdk', 'storage_plugins', 'plugin_host',
                                       'plugin_configuration', 'rag_testing'}),
    'persistence_schema': frozenset({'database_contracts', 'storage_sdk'}),
    'persistence_local': frozenset({'database_contracts', 'database_providers', 'storage_adapters', 'storage_sdk'}),
    'plugin_profiles': frozenset({'plugin_native'}),
    'harness_graph': frozenset({'agent_engine', 'plugin_native'}),
    'harness_context': frozenset({'context_budget'}),
    'harness_model': frozenset({'agent_decisions', 'agent_engine', 'agent_model', 'agent_schema', 'context_budget'}),
    'harness_application': frozenset({'harness_sessions'}),
    'harness_profile': frozenset({'harness_application', 'harness_services', 'plugin_host', 'plugin_profiles',
                                  'model_plugins', 'storage_catalog', 'storage_plugins', 'storage_sdk',
                                  'storage_cache_plugins', 'model_provider_plugins'}),
    'plugin_process': frozenset({'plugin_host', 'plugin_native', 'plugin_configuration'}),
    'plugin_contracts': frozenset({'plugin_host', 'plugin_native', 'plugin_process', 'protocol_validation'}),
    'plugin_deployment': frozenset({'plugin_contracts', 'plugin_native'}),
    'contract_worker': frozenset({'host_protocol', 'host_worker', 'plugin_contracts'}),
    'application_storage': frozenset({'database_contracts', 'knowledge_schema', 'persistence_schema', 'plugin_contracts',
        'plugin_host', 'search', 'session_storage', 'storage_catalog', 'storage_object_wire',
        'storage_json_wire', 'storage_plugins', 'storage_process', 'storage_protocol', 'storage_sdk',
        'storage_service', 'storage_structured_wire'}),
    'application_knowledge': frozenset({'agent_context', 'agent_tools', 'application_composition', 'application_storage', 'authentication', 'context_budget', 'core', 'document_parser', 'embedding_jobs', 'foundation_services', 'knowledge_access', 'knowledge_authority_contract', 'knowledge_capabilities', 'knowledge_contracts', 'knowledge_model_ports', 'knowledge_model_service', 'knowledge_platform_contract', 'knowledge_profile', 'knowledge_sessions', 'knowledge_storage', 'knowledge_storage_configuration', 'plugin_configuration', 'research', 'retrieval_pipeline', 'search_evidence', 'search_router', 'semantic_page_runtime', 'session_identity', 'session_store', 'storage'}),
    'knowledge_model_ports': frozenset({'embeddings', 'knowledge_contracts', 'model_errors',
                                      'ocr_contract', 'plugin_contracts', 'session_identity'}),
    'knowledge_capabilities': frozenset({'agent_context', 'agent_engine', 'foundation_services', 'knowledge_application', 'knowledge_authority_contract', 'knowledge_context', 'knowledge_contracts', 'knowledge_model_service', 'knowledge_platform_contract', 'knowledge_semantic_contract', 'knowledge_service', 'knowledge_workflow', 'plugin_contracts', 'provider_runtime'}),
    'knowledge_contracts': frozenset({'agent_context', 'agent_model', 'model_errors', 'plugin_configuration', 'plugin_contracts',
                                     'retrieval_pipeline', 'session_identity'}),
    'knowledge_context': frozenset({'planning_references', 'session_identity'}),
    'knowledge_access': frozenset({'core', 'knowledge_queries', 'session_identity'}),
    'knowledge_authority_contract': frozenset({'database_contracts', 'knowledge_contracts', 'knowledge_schema',
        'plugin_contracts', 'plugin_native', 'storage_expression_wire', 'storage_sdk', 'storage_structured_wire'}),
    'knowledge_platform_contract': frozenset({'database_contracts', 'documents', 'embeddings',
        'knowledge_contracts', 'model_errors', 'platform_transfers', 'plugin_contracts', 'session_identity', 'storage_sdk'}),
    'platform_transfers': frozenset({'session_identity'}),
    'knowledge_semantic_contract': frozenset({'knowledge_contracts', 'plugin_contracts',
                                             'semantic_pages_v2', 'session_identity'}),
    'ocr_contract': frozenset({'model_errors'}),
    'storage_expression_wire': frozenset({'database_contracts', 'database_process', 'storage_sdk'}),
    'storage_json_wire': frozenset({'contract_transfer', 'database_contracts'}),
    'storage_structured_wire': frozenset({'database_contracts', 'database_process',
                                        'storage_expression_wire', 'storage_json_wire', 'storage_protocol', 'storage_sdk'}),
    'storage_object_wire': frozenset({'blob_service', 'storage_object_transfer', 'storage_process', 'storage_sdk'}),
    'storage_object_transfer': frozenset({'blob_service', 'storage_sdk'}),
    'storage_reset': frozenset({'storage_catalog', 'storage_sdk'}),
    'model_service': frozenset({'agent_engine', 'agent_model', 'llm_protocol', 'model_completion', 'model_sdk'}),
    'agent_tool_registry': frozenset({'agent_protocol'}),
    'agent_builtin_tools': frozenset({'agent_tools', 'agent_tool_registry', 'agent_history'}),
}
HARNESS_MODULES = frozenset({'agent_decisions', 'agent_engine', 'agent_native', 'agent_tool_loop',
                           'agent_plugins', 'plugin_native', 'plugin_host', 'harness_sessions',
                           'harness_loop', 'harness_services', 'context_budget', 'session_identity', 'session_store',
                           'plugin_profiles', 'harness_graph', 'harness_context', 'harness_model',
                           'harness_application', 'harness_profile', 'model_service', 'plugin_process',
                           'plugin_configuration', 'host_protocol', 'host_worker',
                           'plugin_contracts', 'plugin_deployment', 'contract_worker',
                           'llm_contract', 'foundation_services', 'contract_transfer'})
KNOWLEDGE_BUSINESS = frozenset({'core', 'agent_tools', 'semantic_pages_v2', 'embedding_jobs', 'knowledge_access'})
NATIVE_DEPENDENCIES = {'agent-kernel': {'agent-harness'},
                       'agent-harness': {'plugin-runtime', 'plugin-host', 'agent-services',
                                         'llm-service', 'storage-service', 'serde', 'serde_json'},
                       'agent-services': {'serde', 'serde_json'},
                       'llm-service': {'serde', 'serde_json', 'tokio'},
                       'storage-service': {'serde', 'serde_json'},
                       'plugin-host': {'serde', 'serde_json', 'libc', 'windows-sys'},
                       'plugin-runtime': {'serde', 'serde_json', 'tokio'}}


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


def profile_factory_imports(tree: ast.AST, module: str) -> set[int]:
    if module != 'kb_platform.host_worker':
        return set()
    return {node.lineno for definition in ast.walk(tree)
            if isinstance(definition, ast.FunctionDef) and definition.name == '_boot'
            for node in ast.walk(definition)
            if isinstance(node, ast.Call) and ast.unparse(node) == 'importlib.import_module(module)'}


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
            elif name in NATIVE_DEPENDENCIES:
                expected = (root / 'rust' / name).resolve()
                if not spec.get('path') or (directory / spec['path']).resolve() != expected:
                    errors.append(f'{relative}: {name} must resolve to the reviewed workspace crate')
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
            if relative == 'kb_platform/rag_testing/formal_state_machines.py':
                errors.extend(f'{relative}: {error}' for error in formal_parse_reuse_violations(tree))
            if relative == 'scripts/check_project.py':
                errors.extend(f'{relative}: {error}' for error in offline_bootstrap_violations(tree))
            if relative == 'kb_platform/rag_testing/search_router_contracts.py':
                errors.extend(f'{relative}: {error}' for error in search_stage_diagnostic_violations(tree))
            if relative == 'kb_platform/rag_testing/embedded_search_contracts.py':
                errors.extend(f'{relative}: {error}' for error in embedded_stage_diagnostic_violations(tree))
            if relative == 'kb_platform/knowledge_contracts.py':
                errors.extend(f'{relative}: {error}' for error in model_receipt_violations(tree))
                errors.extend(f'{relative}: {error}' for error in model_capacity_outcome_violations(tree))
                errors.extend(f'{relative}: {error}' for error in model_receipt_cleanup_violations(tree))
                errors.extend(f'{relative}: {error}' for error in model_error_projection_violations(tree))
            errors.extend(f'{relative}: {error}' for error in search_probe_diagnostic_violations(tree, relative))
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


def formal_parse_reuse_violations(tree: ast.AST) -> list[str]:
    expected = ast.parse(r"""def python_symbols(source: str) -> dict[str, ast.AST]:
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

def local_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Repository-relative nonempty path required')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f'Missing or escaping repository file: {relative}')
    return path

def source_digest(root: Path, binding: dict) -> str:
    return _source_digest(root, binding, python_symbols)

def _source_digest(root: Path, binding: dict, parse_symbols) -> str:
    path = local_file(root, binding['path'])
    source = path.read_text('utf-8')
    if binding['language'] == 'python':
        symbols = parse_symbols(source)
        if binding['symbol'] not in symbols:
            raise ValueError(f"Missing Python symbol: {binding['symbol']}")
        options = {'show_empty': True} if sys.version_info >= (3, 13) else {}
        return digest(ast.dump(symbols[binding['symbol']], include_attributes=False, **options).encode())
    if binding['language'] == 'javascript-module':
        if path.suffix != '.mjs' or binding['symbol'] != '<module>':
            raise ValueError('JavaScript bindings require a complete .mjs module')
        return digest(path.read_bytes().replace(b'\r\n', b'\n'))
    if binding['language'] != 'rust':
        raise ValueError('Source binding language must be python, rust or javascript-module')
    leaf = binding['symbol'].replace('::', '.').rsplit('.', 1)[-1]
    if leaf != '<module>' and (not re.search('\\b(?:fn|enum|struct)\\s+' + re.escape(leaf) + '\\b', source)):
        raise ValueError(f"Missing Rust declaration: {binding['symbol']}")
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

def registry_violations(root: Path, registry: dict | None=None) -> list[str]:
    with _source_digest_session() as reviewed_digest:
        return _registry_violations(root, registry, reviewed_digest)
""")
    errors = []
    protected = {node.name for node in expected.body}
    for reference in expected.body:
        actual = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == reference.name]
        if len(actual) != 1 or ast.dump(actual[0]) != ast.dump(reference):
            errors.append(f'formal parse reuse: preserve fresh reads and invocation-local content cache in {reference.name}')
    symbols = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'python_symbols']
    if len(symbols) != 1 or symbols[0].decorator_list:
        errors.append('formal parse reuse: public python_symbols must return independent uncached ASTs')
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in protected | {'python_symbols'} for target in targets):
                errors.append('formal parse reuse: no module-scope replacement of validation functions')
    workers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_registry_violations']
    call = ast.parse('actual = reviewed_digest(root, binding)').body[0]
    if len(workers) != 1 or sum(ast.dump(node) == ast.dump(call) for node in ast.walk(workers[0])) != 1:
        errors.append('formal parse reuse: registry bindings require the scoped digest callback')
    return errors


def search_diagnostic_digest(node):
    options = {'show_empty': True} if sys.version_info >= (3, 13) else {}
    return hashlib.sha256(ast.dump(node, include_attributes=False, **options).encode()).hexdigest()


def search_probe_diagnostic_violations(tree: ast.AST, relative: str) -> list[str]:
    bindings = {'kb_platform/rag_testing/search_stage_probe.py': ('<module>', '3923c10cd2a749a43029ef30cbe72690ff8e44b4d177ea12368b24523252e5aa'), 'kb_platform/rag_testing/search_stage_diagnostics.py': ('<module>', '6f54daeb406baa015c0d0ae9d7b92cfbf39d22e671d535ec841c3762f7d2cb6f'), 'kb_platform/rag_testing/search_stage_probe_contracts.py': ('SearchStageProbeContracts.test_real_three_case_single_pass_retains_stage_observations', '09623de92d7ecb79f2be3ada1065475844a98d21668ec126c4b5716050e550f7')}
    expected = bindings.get(relative)
    if expected is None:
        return []
    nodes = [tree] if expected[0] == '<module>' else [
        node for owner in tree.body if isinstance(owner, ast.ClassDef) and owner.name == 'SearchStageProbeContracts'
        for node in owner.body if isinstance(node, ast.FunctionDef)
        and node.name == 'test_real_three_case_single_pass_retains_stage_observations']
    if len(nodes) != 1 or search_diagnostic_digest(nodes[0]) != expected[1]:
        return ['controlled search diagnostics require reviewed bounded metadata, single-pass stage order and original errors']
    return []


def search_stage_diagnostic_violations(tree: ast.AST) -> list[str]:
    expected = ast.parse('def stage(self):\n'
        '    try:\n'
        '        self.router.stage(self.index, self.units, vector_dimension=3)\n'
        '    except Exception as error:\n'
        '        note_stage_failure(error, self.root)\n'
        '        raise\n').body[0]
    stages = [node for owner in tree.body if isinstance(owner, ast.ClassDef)
              and owner.name == 'SearchRouterContracts' for node in owner.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'stage']
    if len(stages) != 1 or ast.dump(stages[0]) != ast.dump(expected):
        return ['search-stage diagnostics require one original call, failure-only note, and bare raise without retry']
    return []


def embedded_stage_diagnostic_violations(tree: ast.AST) -> list[str]:
    expected = ast.parse('def _stage(self, backend, index, units, vector_dimension):\n'
        '    try:\n'
        '        return backend.stage(index, units, vector_dimension)\n'
        '    except Exception as error:\n'
        '        note_stage_failure(error, Path(self.folder.name))\n'
        '        raise\n').body[0]
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'EmbeddedSearchContracts']
    if len(owners) != 1:
        return ['embedded-stage diagnostics require the original fixture owner']
    stages = [node for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name == '_stage']
    if len(stages) != 1 or ast.dump(stages[0]) != ast.dump(expected):
        return ['embedded-stage diagnostics require one original call, owned-root failure-only note, and bare raise without retry']
    direct = [node for node in ast.walk(owners[0]) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == 'stage']
    if len(direct) != 1 or direct[0] not in tuple(ast.walk(stages[0])):
        return ['every embedded fixture stage call must use the failure-only wrapper, including setup and generation loop']
    expected_calls = {
        'setUp': 'self._stage(self.backend, self.index, self.authority.units, 3)',
        'test_exact_vectors_and_seal': 'self._stage(self.backend, self.index, self.authority.units, 3)',
        'test_failed_staging_not_visible': "self._stage(self.backend, index, [{**self.authority.units[0], 'vector': [float('inf'), 0, 0]}], 3)",
        'test_pages_do_not_change_block_scores': "self._stage(self.backend, index, [u for u in self.authority.units if u['kind'] != 'Page'], 3)",
        'test_closed_immutable_readers_release_generations_for_immediate_removal': 'self._stage(backend, index, self.authority.units, 3)',
    }
    actual = {node.name: [ast.dump(call) for call in ast.walk(node) if isinstance(call, ast.Call)
                         and isinstance(call.func, ast.Attribute) and call.func.attr == '_stage']
              for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name != '_stage'}
    actual = {name: calls for name, calls in actual.items() if calls}
    expected_calls = {name: [ast.dump(ast.parse(call, mode='eval').body)] for name, call in expected_calls.items()}
    if actual != expected_calls:
        return ['all five original embedded stage call sites must retain their original receiver, arguments and call count']
    return []
def model_receipt_violations(tree: ast.AST) -> list[str]:
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ModelEndpoint']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'complete']
    candidates = [node.value for method in methods for node in ast.walk(method)
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                      and target.id == 'expired' for target in node.targets)]
    expected = {'envelope is not None', 'deadline < monotonic()'}

    def conjuncts(node):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return {value for child in node.values for value in conjuncts(child)}
        return {ast.unparse(node)}

    if len(candidates) == 1 and isinstance(candidates[0], ast.ListComp):
        candidate = candidates[0]
        if len(candidate.generators) == 1:
            generator = candidate.generators[0]
            conditions = {value for condition in generator.ifs for value in conjuncts(condition)}
            if (ast.unparse(generator.iter) == 'self._pending.items()'
                    and ast.unparse(generator.target) == '(key, (deadline, envelope))'
                    and ast.unparse(candidate.elt) == 'key' and expected <= conditions):
                return []
    return ['model receipt expiry must retain in-flight placeholders and only prune expired completed envelopes']


def model_capacity_outcome_violations(tree: ast.AST) -> list[str]:
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ModelEndpoint']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'complete']
    guards = [node for method in methods for node in ast.walk(method)
              if isinstance(node, ast.If) and ast.unparse(node.test) == 'len(self._pending) >= 64']
    invocations = [node for method in methods for node in ast.walk(method)
                   if isinstance(node, ast.Call) and ast.unparse(node.func) == 'self._model.complete']
    if (len(guards) == len(invocations) == 1 and guards[0].lineno < invocations[0].lineno
            and len(guards[0].body) == 1 and isinstance(guards[0].body[0], ast.Raise)):
        call = guards[0].body[0].exc
        if isinstance(call, ast.Call) and ast.unparse(call.func) == 'ContractError':
            for keyword in call.keywords:
                if keyword.arg == 'details':
                    try:
                        details = ast.literal_eval(keyword.value)
                    except (ValueError, TypeError):
                        break
                    if (isinstance(details, dict) and details.get('model_sent') is False
                            and isinstance(details.get('call_record'), dict)
                            and type(details['call_record'].get('attempts')) is int
                            and details['call_record']['attempts'] == 0
                            and details['call_record'].get('model_sent') is False):
                        return []
    return ['model receipt capacity refusal must expose known-unsent zero attempts before model invocation']


def model_receipt_cleanup_violations(tree: ast.AST) -> list[str]:
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ModelEndpoint']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'complete']
    blocks = [node for method in methods for node in method.body if isinstance(node, ast.Try)]
    if len(blocks) == 1 and len(blocks[0].handlers) == 1:
        handler = blocks[0].handlers[0]
        expected = ast.parse('with self._lock:\n    self._pending.pop(token, None)\n'
                             'if not isinstance(error, Exception):\n    raise\n').body
        if (handler.type is not None and ast.unparse(handler.type) == 'BaseException'
                and handler.name == 'error' and len(handler.body) >= 2
                and all(ast.dump(actual, include_attributes=False) == ast.dump(required, include_attributes=False)
                        for actual, required in zip(handler.body[:2], expected, strict=True))):
            return []
    return ['model receipt failures must clean BaseException placeholders then bare-raise cancellation identity']


def model_error_projection_violations(tree: ast.AST) -> list[str]:
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ModelEndpoint']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'complete']
    blocks = [node for method in methods for node in method.body if isinstance(node, ast.Try)]
    expected = ast.parse('from .model_errors import error_details\n'
        'details = error_details(error)\n'
        "if not hasattr(error, 'code'):\n"
        "    raise ContractError('CONTRACT_IMPLEMENTATION_FAILED', 'Service implementation failed',\n"
        "        details={**details, 'code': 'CONTRACT_IMPLEMENTATION_FAILED'}) from error\n").body
    if len(blocks) == 1 and len(blocks[0].handlers) == 1:
        handler = blocks[0].handlers[0]
        if (len(handler.body) >= 5
                and all(ast.dump(actual, include_attributes=False) == ast.dump(required, include_attributes=False)
                        for actual, required in zip(handler.body[2:5], expected, strict=True))):
            return []
    return ['generic model failures must preserve canonical facts with fixed safe public code and message']


def offline_bootstrap_violations(tree: ast.AST) -> list[str]:
    errors = []
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    expected_seal = ast.parse('machine = platform.machine()\nsys.addaudithook(deny_external_io)\nreturn machine').body
    expected_prefix = ast.parse("if kind == 'contracts' and shard is None:\n    return run_contract_workers(output)\n"
        'sys.path.insert(0, str(ROOT))\nmachine = seal_offline_worker()').body
    def dump(nodes):
        return [ast.dump(node, include_attributes=False) for node in nodes]
    seal = functions.get('seal_offline_worker')
    worker = functions.get('run_worker')
    no_args = ast.parse('def seal_offline_worker(): pass').body[0].args
    if seal is None or dump([seal.args]) != dump([no_args]) or seal.decorator_list or dump(seal.body) != dump(expected_seal):
        errors.append('offline bootstrap must read only real stdlib metadata then install the unchanged audit hook')
    if worker is None or worker.decorator_list or dump(worker.body[:len(expected_prefix)]) != dump(expected_prefix):
        errors.append('offline workers must finish bootstrap and seal before any project/test discovery')
    startup = []

    def collect_startup(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in node.decorator_list + node.args.defaults + [value for value in node.args.kw_defaults if value is not None]:
                collect_startup(child)
            return
        startup.append(node)
        for child in ast.iter_child_nodes(node):
            collect_startup(child)

    for node in tree.body:
        collect_startup(node)
    for node in startup:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or '']
        else:
            modules = []
        if any(module.split('.')[0] not in sys.stdlib_module_names for module in modules):
            errors.append('checker startup cannot import project or third-party modules before the audit seal')
        if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == '__import__' or
                isinstance(node.func, ast.Attribute) and node.func.attr in
                {'import_module', 'loadTestsFromName', 'loadTestsFromModule', 'discover'}):
            errors.append('checker startup cannot dynamically import or discover modules before the audit seal')
    coordinator = functions.get('run_contract_workers')
    if coordinator is not None:
        for node in ast.walk(coordinator):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                errors.append('the unsealed contract coordinator cannot import project/test modules')
            if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Attribute) and node.func.attr in
                    {'loadTestsFromName', 'loadTestsFromModule', 'discover', 'import_module'} or
                    isinstance(node.func, ast.Name) and node.func.id == '__import__'):
                errors.append('contract discovery belongs only in the sealed worker')
    return sorted(set(errors))


def workflow_violations(workflow):
    jobs = workflow.get('jobs', {})
    integration = jobs.get('integration', {})
    required = jobs.get('required', {})
    errors = []
    dependencies = {'quality': 'QUALITY_RESULT', 'secrets': 'SECRETS_RESULT',
                    'integration': 'INTEGRATION_RESULT', 'formal': 'FORMAL_RESULT'}
    if not dependencies.keys() <= set(required.get('needs', [])):
        errors.append('required must depend on quality, secrets, integration and formal')
    if required.get('if') != 'always()' or required.get('continue-on-error', False):
        errors.append('required must always evaluate every job result')
    if not integration or 'if' in integration or integration.get('continue-on-error', False):
        errors.append('integration must run as a mandatory job')
    commands = [step.get('run', '') for step in integration.get('steps', [])
                if 'if' not in step and not step.get('continue-on-error', False)]
    for command in (
            'uv run --no-sync python scripts/check_project.py --local-integration --storage-integration --output reports/ci/integration-check',
            'uv run --no-sync python -m kb_platform.rag_testing.browser_integration --checker-summary reports/ci/integration-check/summary.json --output reports/ci/browser'):
        if command not in commands:
            errors.append('integration must run required command: ' + command)
    checks = [step for step in required.get('steps', [])
              if 'if' not in step and not step.get('continue-on-error', False)]
    formal = jobs.get('formal', {})
    if not formal or 'if' in formal or formal.get('continue-on-error', False):
        errors.append('formal model validation must run as a mandatory job')
    formal_commands = [step.get('run', '') for step in formal.get('steps', [])
                       if 'if' not in step and not step.get('continue-on-error', False)]
    command = 'uv run --no-sync python scripts/check_project.py --formal-only --output reports/ci/formal'
    if command not in formal_commands:
        errors.append('formal must run the bounded model validation command')
    aggregate = ('test "$QUALITY_RESULT" = success && test "$SECRETS_RESULT" = success && '
                 'test "$INTEGRATION_RESULT" = success && test "$FORMAL_RESULT" = success')
    if not any(all(step.get('env', {}).get(variable) == '${{ needs.' + job + '.result }}'
                   for job, variable in dependencies.items())
               and step.get('run', '') == aggregate for step in checks):
        errors.append('required must reject every failed or skipped gate')
    return errors



def storage_boundary_violations(tree, module):
    portable = module.rsplit('.', 1)[-1] in {
        'knowledge_schema', 'knowledge_sql', 'knowledge_storage', 'session_store',
        'persistence_schema', 'persistence_journal', 'provider_receipt_database', 'budget', 'cache'}
    sdk = module.startswith('kb_platform.storage_sdk.')
    assembly = module in {'kb_platform.platform_plugins', 'kb_platform.application_composition'}
    errors = []
    for node in ast.walk(tree):
        if module == 'kb_platform.search' and isinstance(node, ast.Attribute) and node.attr == '__name__':
            errors.append((node.lineno, 'search dependencies must be declared by implementations, not selected by class name'))
        if (portable or sdk) and isinstance(node, ast.Attribute) and node.attr in {
                'engine', 'physical_name', 'runtime_engine', 'migration_engine', 'dialect'}:
            errors.append((node.lineno, 'portable storage consumers must not inspect provider internals'))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if (portable or sdk) and node.value.lower() in {'postgresql', 'postgres', 'mysql', 'sqlite'}:
                errors.append((node.lineno, 'portable storage contracts cannot select a database vendor'))
            if assembly and node.value.startswith(('platform.repository.postgres', 'platform.search.postgres',
                    'platform.search.bm25.', 'platform.search.dense.', 'platform.search.embedded',
                    'platform.search.opensearch', 'platform.search.split')):
                errors.append((node.lineno, 'compose storage by declared slots instead of vendor plugin keys'))
    for line, target in imports(tree, module):
        if (portable or sdk or assembly) and (target.split('.')[0] in {'psycopg', 'pymysql'}
                or target.startswith(('sqlalchemy.dialects', 'kb_platform.database_providers',
                    'kb_platform.database_service', 'kb_platform.postgres_search', 'kb_platform.storage_adapters'))):
            errors.append((line, 'inject a storage contract; provider compatibility belongs in its adapter'))
    return errors


def offline_transport_violations(root: Path) -> list[str]:
    directory = root / 'kb_platform/rag_testing'
    helper = directory / 'offline_transport.py'
    if not helper.is_file() and not (directory / 'embedding_jobs_contracts.py').is_file():
        return []
    if not helper.is_file():
        return ['offline fixtures require the controlled real-client constructor']
    tree = ast.parse(helper.read_text('utf-8'))
    required = (
        '_REAL_CLIENT = httpx.Client',
        "raise AssertionError('Offline fixture sent before its capture transport was installed')",
        "if any(name in kwargs for name in ('transport', 'proxy', 'mounts')):\n"
        "    raise ValueError('Offline construction cannot replace an explicit transport or proxy')",
        'client = _REAL_CLIENT(*args, **kwargs, transport=httpx.MockTransport(reject_uncaptured_send))',
        'clients.append(client)',
        'return client',
        "with patch('kb_platform.remote.httpx.Client', new=construct):\n    yield",
        'except BaseException:\n    for client in clients:\n        client.close()\n    raise',
    )
    forms = {ast.dump(node, include_attributes=False) for node in ast.walk(tree)}
    errors = []
    for statement in required:
        parsed = ast.parse('try:\n    pass\n' + statement if statement.startswith('except ') else statement)
        target = parsed.body[0].handlers[0] if statement.startswith('except ') else parsed.body[0]
        if ast.dump(target, include_attributes=False) not in forms:
            errors.append('offline constructor must preserve real-client injection, fail-fast and cleanup semantics')
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else ','.join(a.name for a in node.names)
            if module not in {'httpx', 'contextlib', 'unittest.mock'}:
                errors.append('offline constructor must not access process environment or alternate transports')
        if isinstance(node, ast.Call) and ast.unparse(node.func) not in {
            'AssertionError', 'ValueError', 'any', '_REAL_CLIENT', 'httpx.MockTransport',
            'clients.append', 'patch', 'client.close',
        }:
            errors.append('offline constructor cannot invoke environment mutation or replacement client factories')
        if isinstance(node, ast.keyword) and node.arg in {'trust_env', 'proxy', 'mounts'}:
            errors.append('offline constructor must preserve original HTTPX environment policy')
    entries = {
        'embedding_jobs_contracts.py': ('captured_google_fixture', 'GoogleEmbeddingProvider'),
        'model_review_contracts.py': ('observed', 'GoogleEmbeddingProvider'),
        'model_embedding_review_contracts.py': ('observed', 'GoogleEmbeddingProvider'),
        'plugin_configuration_contracts.py': (
            'test_invalid_referenced_remote_provider_does_not_close_existing_clients', 'PluginHost'),
    }
    for filename, (function, constructor) in entries.items():
        path = directory / filename
        if not path.is_file():
            errors.append(f'{filename}: missing controlled offline fixture')
            continue
        module = ast.parse(path.read_text('utf-8'))
        candidates = [node for node in ast.walk(module)
                      if isinstance(node, ast.FunctionDef) and node.name == function]
        if len(candidates) != 1:
            errors.append(f'{filename}: offline fixture entry must remain explicit')
            continue
        calls = [node for node in ast.walk(candidates[0]) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == constructor]
        protected = set()
        for node in ast.walk(candidates[0]):
            if isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == 'offline_client_construction' for item in node.items
            ):
                for statement in node.body:
                    protected.update(id(child) for child in ast.walk(statement))
        if not calls or any(id(call) not in protected for call in calls):
            errors.append(f'{filename}: install fail-fast transport before constructing {constructor}')
    return sorted(set(errors))


def exact_history_fingerprint_violations(root: Path) -> list[str]:
    path = root / '.gitleaksignore'
    if not (root / 'formal/combination-inputs-v1.json').is_file() and not path.exists():
        return []
    prefix = '6efd45e2129b0c0a2afa7a6d51fa60a561f69bc4:formal/combination-inputs-v1.json:'
    coordinates = [('generic-api-key', line) for line in (3917, 3918, 3919, 3981, 4052, 4081, 4082, 4091, 4092, 4093)]
    coordinates.append(('sentry-access-token', 3904))
    expected = [f'{prefix}{rule}:{line}' for rule, line in coordinates]
    try:
        actual = path.read_text('utf-8').splitlines()
    except OSError:
        actual = []
    if actual != expected:
        return ['Only the eleven authorized immutable historical model-digest fingerprints may be ignored']
    return []


def architecture_violations(root: Path) -> list[str]:

    import yaml

    errors = []
    workflow_path = root / '.github/workflows/ci.yml'
    if workflow_path.is_file():
        errors.extend(workflow_violations(yaml.safe_load(workflow_path.read_text('utf-8'))))
    sources = sorted((root / "kb_platform").rglob("*.py"))
    if not sources:
        return ["No active product sources found"]
    for path in sources:
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts)
        tree = ast.parse(path.read_text("utf-8-sig"), filename=str(relative))
        errors.extend(f'{relative.as_posix()}:{line}: {reason}'
                      for line, reason in storage_boundary_violations(tree, module))
        if path.name.endswith("_contracts.py"):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    errors.append(f"{relative.as_posix()}:{node.lineno}: use unittest assertions in contracts")
        evaluation = module.startswith("kb_platform.rag_testing.")
        if not evaluation and path.stem in KNOWLEDGE_BUSINESS:
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'execute' and (not node.args
                        or not isinstance(node.args[0], ast.Attribute)
                        or not isinstance(node.args[0].value, ast.Name)
                        or node.args[0].value.id != 'KnowledgeQuery')):
                    errors.append(f'{relative.as_posix()}:{node.lineno}: knowledge business must use a declared domain operation')
        foundation = module.split(".")[-1] in FOUNDATIONS and evaluation
        shared = module.split(".")[-1] in SHARED and evaluation
        for line, target in imports(tree, module):
            top = target.split(".")[0]
            reason = None
            layer = AGENT_DEPENDENCIES.get(path.stem) if not evaluation else None
            if module.startswith('kb_platform.storage_sdk.'):
                layer = frozenset({'storage_sdk', 'database_contracts', 'protocol_validation'})
            elif module.startswith('kb_platform.storage_adapters.'):
                layer = frozenset({'storage_sdk', 'storage_adapters', 'database_contracts',
                                   'database_migrations', 'database_providers', 'database_process',
                                   'blob_service', 'storage', 'search_errors', 'search_tokens', 'implementation_dependencies'})
            elif module.startswith('kb_platform.database_providers.'):
                layer = frozenset({'database_contracts', 'database_providers'})
            elif module.startswith('kb_platform.session_storage.'):
                layer = frozenset({'database_contracts', 'session_storage', 'storage_sdk'})
            if layer is not None and target.startswith('kb_platform.') and target.split('.')[1] not in layer:
                reason = 'agent boundary cannot depend on this implementation; inject its port at composition'
            if (path.stem in HARNESS_MODULES | {'agent_model', 'agent_provider'}
                    and top in {'openai', 'httpx', 'fastapi', 'requests', 'psycopg'}):
                reason = 'agent contracts and model policy cannot depend on transport or API frameworks'
            if top in EXCLUDED_IMPORTS:
                reason = "generated artifacts cannot be runtime dependencies"
            elif (not evaluation and path.stem in KNOWLEDGE_BUSINESS
                    and (top in {'psycopg', 'sqlalchemy', 'sqlite3'} or target.startswith(tuple(
                        'kb_platform.' + name for name in ('database_providers', 'database_service',
                                                         'knowledge_sql', 'knowledge_schema', 'knowledge_storage'))))):
                reason = 'knowledge business cannot depend on database drivers or persistence implementations'
            elif (not evaluation or shared) and top == "scripts":
                reason = "product code cannot import CLI scripts"
            elif (not evaluation or shared) and target.startswith("kb_platform.rag_testing."):
                if target.split(".")[2] not in SHARED:
                    reason = "product code can only use shared RAG contracts, cache, budget, and capture"
            if foundation and not reason and target.startswith("kb_platform."):
                parts = target.split(".")
                if len(parts) >= 2 and parts[1] in DATABASE_FOUNDATIONS:
                    continue
                if len(parts) >= 3 and parts[1] == "rag_testing" and parts[2] in FOUNDATIONS:
                    continue
                if target != "kb_platform.rag_testing":
                    reason = "RAG foundations cannot depend on product execution or evaluation"
            if (target == "<dynamic-import>" and (not evaluation or shared)
                    and line not in profile_factory_imports(tree, module)):
                reason = "dynamic runtime imports need a statically reviewable module name"
            if reason:
                errors.append(f"{relative.as_posix()}:{line}: {target}: {reason}")
        if not evaluation or shared:
            for line in path_mutations(tree):
                errors.append(f"{relative.as_posix()}:{line}: product code cannot mutate sys.path")
    if (root / "tests").exists():
        errors.append("Keep contract checks in kb_platform/rag_testing; do not restore a parallel tests directory")
    errors.extend(offline_transport_violations(root))
    errors.extend(exact_history_fingerprint_violations(root))
    errors.extend(source_quality_violations(root))
    errors.extend(native_architecture_violations(root))
    return sorted(set(errors))


def deny_external_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
                 "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        raise RuntimeError(f"OFFLINE_CHECK_FORBIDS_{event}")


def seal_offline_worker():
    machine = platform.machine()
    sys.addaudithook(deny_external_io)
    return machine


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


class ContractTimingResult(unittest.TextTestResult):
    def __init__(self, *args, timing, **kwargs):
        super().__init__(*args, **kwargs)
        self._timing = timing
        self._outcomes = {'ERROR': self.errors, 'FAIL': self.failures, 'SKIP': self.skipped,
            'EXPECTED_FAILURE': self.expectedFailures, 'UNEXPECTED_SUCCESS': self.unexpectedSuccesses}

    def _record(self, test, event, status):
        self._timing.write(json.dumps({'test': test.id(), 'event': event, 'status': status,
            'monotonic': time.monotonic(), 'process_time': time.process_time()}) + '\n')
        self._timing.flush()

    def startTest(self, test):
        super().startTest(test)
        self._counts = {name: len(outcomes) for name, outcomes in self._outcomes.items()}
        self._status = 'INCOMPLETE'
        self._record(test, 'start', 'RUNNING')

    def addSuccess(self, test):
        super().addSuccess(test)
        self._status = 'PASS'

    def stopTest(self, test):
        status = next((name for name, outcomes in self._outcomes.items()
                       if len(outcomes) > self._counts[name]), self._status)
        try:
            self._record(test, 'stop', status)
        finally:
            super().stopTest(test)


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
    machine = seal_offline_worker()
    write_json(output / 'worker-bootstrap.json', {'platform_machine': machine,
        'states': ['BOOTSTRAP', 'SEALED', 'DISCOVER'], 'audit_policy': 'deny_external_io',
        'scope': 'Only real stdlib platform.machine metadata is read before sealing; project/test imports follow the seal.'})
    if kind == "contracts":
        faulthandler.dump_traceback_later(150)
        try:
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
            with (output / 'contracts-timing.jsonl').open('w', encoding='utf-8') as timing:
                result = unittest.TextTestRunner(verbosity=2,
                    resultclass=lambda *args, **kwargs: ContractTimingResult(*args, timing=timing, **kwargs)).run(suite)
            passed = result.wasSuccessful() and not result.skipped and not result.expectedFailures
            write_json(output / "contracts.json", {
                "decision": "PASS" if passed else "FAIL", "modules": counts, "total": result.testsRun,
                "discovered": discovered, "selected": identities,
                "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
                "expected_failures": len(result.expectedFailures),
                "scope": "offline component contracts; no production RAG quality claim",
            })
            return 0 if passed else 1
        finally:
            faulthandler.cancel_dump_traceback_later()
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


def host_binary_path(target: Path) -> Path:
    return target / 'release' / ('evidence-host.exe' if sys.platform == 'win32' else 'evidence-host')


def snapshot_host_binary(build: CheckResult, target: Path, output: Path) -> Path | None:
    if build.exit_code:
        return None
    source = host_binary_path(target)
    destination = output / 'native' / source.name
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def native_worker_path(target: Path) -> Path:
    return target / 'release/examples' / ('native_worker.exe' if sys.platform == 'win32' else 'native_worker')


def snapshot_native_worker(build: CheckResult, target: Path, output: Path) -> Path | None:
    if build.exit_code:
        return None
    source = native_worker_path(target)
    destination = output / 'native' / source.name
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def snapshot_contract_provider(build: CheckResult, target: Path, output: Path) -> Path | None:
    if build.exit_code:
        return None
    source = target / 'release/examples' / ('contract_provider.exe' if sys.platform == 'win32' else 'contract_provider')
    destination = output / 'native' / source.name
    destination.parent.mkdir(exist_ok=True)
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
    parser.add_argument('--formal-only', action='store_true',
                        help='Run the additional bounded TLC gate; this does not run or replace ordinary quality gates.')
    parser.add_argument('--local-integration', action='store_true',
                        help='Run controlled loopback HTTP/parser/PostgreSQL checks using KB_INTEGRATION_DSN.')
    parser.add_argument('--storage-integration', action='store_true',
                        help='Run controlled actual PostgreSQL/MySQL/SQLite/S3 storage acceptance using KB_STORAGE_TEST_* variables.')
    args = parser.parse_args()
    if args.formal_only and (args.worker or args.local_integration or args.storage_integration):
        parser.error('--formal-only cannot be combined with other gate modes')
    integration_dsn = os.environ.get('KB_INTEGRATION_DSN') if args.local_integration else None
    integration_migration_dsn = os.environ.get('KB_INTEGRATION_MIGRATION_DSN') if args.local_integration else None
    if args.local_integration and not (integration_dsn and integration_migration_dsn):
        parser.error('--local-integration requires KB_INTEGRATION_DSN and KB_INTEGRATION_MIGRATION_DSN')
    storage_env = {key: value for key, value in os.environ.items() if key.startswith('KB_STORAGE_TEST_')}
    output = args.output.resolve()
    if args.worker:
        return run_worker(args.worker, output, args.contract_shard)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("--output must be a new directory; previous results are read-only")
    if args.formal_only:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("KB_", "EVIDENCE_KB_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "AZURE_"))}
        env.update(KB_DEFER_DEFAULT_APP='1', KB_REMOTE_MODELS='0', KB_ENABLE_EMBEDDINGS='0', PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        result = run_check('formal', [sys.executable, '-B', '-m', 'kb_platform.rag_testing.formal_state_machines',
            '--output', str(output / 'formal')], output, env, timeout=900)
        write_json(output / 'summary.json', {'decision': 'PASS' if result.exit_code == 0 else 'FAIL',
            'checks': [asdict(result)], 'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0,
            'scope': 'Additional finite formal-model gate only; ordinary project checks are separately required.'})
        return int(result.exit_code != 0)
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
        results.append(run_check('formal-lint', [sys.executable, '-B', '-m',
            'kb_platform.rag_testing.formal_state_machines', '--lint-only',
            '--output', str(output / 'formal-lint')], output, env))
        results.append(run_check('browser-source-lint', [sys.executable, '-B', '-m',
            'kb_platform.rag_testing.browser_sources'], output, env))
        target = ROOT / "target/checks" / uuid4().hex
        cargo = ["cargo", "--color", "never"]
        results.append(run_check("rust-format", cargo + ["fmt", "--all", "--", "--check"], output, env))
        native_args = ["--workspace", "--locked", "--offline", "--target-dir", str(target)]
        results.append(run_check("rust-clippy", cargo + ["clippy", *native_args, "--all-targets",
                                                        "--", "-D", "warnings"], output, env, timeout=600))
        build = run_check("rust-build", cargo + ["build", "--release", "--examples", "--bins", "--lib", *native_args],
                          output, env, timeout=600)
        results.append(build)
        native_library = None
        host_binary = None
        native_worker = None
        contract_provider = None
        block_reason = "the current Rust release build failed"
        try:
            native_library = snapshot_native_library(build, target, output)
        except OSError as exc:
            block_reason = f"the current Rust release library could not be copied: {exc}"
        try:
            host_binary = snapshot_host_binary(build, target, output)
            native_worker = snapshot_native_worker(build, target, output)
            contract_provider = snapshot_contract_provider(build, target, output)
        except OSError as exc:
            (output / 'host-integration.log').write_text(f'BLOCKED: current host binary unavailable: {exc}\n',
                                                       encoding='utf-8')
        if host_binary is None or native_worker is None or native_library is None:
            results.append(CheckResult('host-integration', 2, 'host-integration.log'))
            if not (output / 'host-integration.log').exists():
                (output / 'host-integration.log').write_text(f'BLOCKED: {block_reason}\n', encoding='utf-8')
        results.append(run_check("rust-tests", cargo + ["test", *native_args], output, env, timeout=600))
        if native_library is not None:
            env["KB_AGENT_KERNEL_LIBRARY"] = str(native_library)
            if contract_provider is not None:
                env['KB_CONTRACT_PROVIDER'] = str(contract_provider)
            results.append(run_check('plugin-process-integration',
                [sys.executable, '-B', '-m', 'kb_platform.rag_testing.plugin_process_integration',
                 '--output', str(output / 'plugin-process-integration')], output, env))
            results.append(run_check('foundation-integration',
                [sys.executable, '-B', '-m', 'kb_platform.rag_testing.foundation_integration',
                 '--output', str(output / 'foundation-integration')], output, env))
        else:
            (output / 'foundation-integration.log').write_text(f'BLOCKED: {block_reason}\n', encoding='utf-8')
            results.append(CheckResult('foundation-integration', 2, 'foundation-integration.log'))
        if host_binary is not None and native_worker is not None and native_library is not None:
            env['KB_PLUGIN_HOST_BINARY'] = str(host_binary)
            env['KB_PLUGIN_HOST_NATIVE_WORKER'] = str(native_worker)
            results.append(run_check('host-integration',
                [sys.executable, '-B', '-m', 'kb_platform.rag_testing.host_integration',
                 '--output', str(output / 'host-integration')], output, env))
        if args.local_integration:
            if native_library is None:
                results.append(run_check('runtime-integration', [sys.executable, '-c',
                    'raise RuntimeError("current native build is required for integration")'], output, env))
            else:
                integration_env = {**env, 'KB_INTEGRATION_DSN': integration_dsn,
                                   'KB_INTEGRATION_MIGRATION_DSN': integration_migration_dsn}
                results.append(run_check('runtime-integration',
                    [sys.executable, '-B', '-m', 'kb_platform.rag_testing.runtime_integration',
                     '--output', str(output / 'runtime-integration')], output, integration_env))
        if args.storage_integration:
            if native_library is None or host_binary is None:
                results.append(run_check('storage-integration', [sys.executable, '-c',
                    'raise RuntimeError("current native and Rust host build is required for storage acceptance")'], output, env))
            else:
                results.append(run_check('storage-integration',
                    [sys.executable, '-B', '-m', 'kb_platform.rag_testing.storage_integration',
                     '--output', str(output / 'storage-integration')], output, {**env, **storage_env}, timeout=600))
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
        "host_binary": str(host_binary) if host_binary is not None else None,
        "native_worker": str(native_worker) if native_worker is not None else None,
        "controlled_local_integration": args.local_integration,
        "controlled_storage_integration": args.storage_integration,
        "scope": ("offline gates plus foundation application and controlled local HTTP/parser/PostgreSQL integration; components and scorer controls reported separately"
                  if args.local_integration else
                  "offline contracts plus controlled localhost host and foundation application integration; component checks and scorer controls reported separately")})
    print(f"{'PASS' if passed else 'FAIL'}: {output / 'summary.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

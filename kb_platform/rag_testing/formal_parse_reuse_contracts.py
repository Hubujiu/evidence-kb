import ast
from copy import deepcopy
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_project import formal_parse_reuse_violations
from . import formal_state_machines as formal
from . import formal_state_machine_contracts as fixtures


class FormalParseReuseContracts(unittest.TestCase):
    def binding(self, symbol='Runtime.step'):
        return {'path': 'kb_platform/lifecycle.py', 'language': 'python', 'symbol': symbol}

    def test_content_reuse_preserves_each_path_check_and_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures.FormalRegistryContracts().fixture(root)
            real_read = Path.read_text
            reads = []

            def read(path, *args, **kwargs):
                reads.append(path)
                return real_read(path, *args, **kwargs)

            with patch.object(formal, 'python_symbols', wraps=formal.python_symbols) as parses, \
                    patch.object(formal, 'local_file', wraps=formal.local_file) as paths, \
                    patch.object(Path, 'read_text', read), formal._source_digest_session() as digest:
                first = digest(root, self.binding())
                self.assertIsInstance(first, str)
                self.assertEqual(first, digest(root, self.binding()))
                digest(root, self.binding('Runtime'))
                self.assertEqual(1, parses.call_count)
                self.assertEqual(3, paths.call_count)
                self.assertEqual(3, len(reads))
            with patch.object(formal, 'python_symbols', wraps=formal.python_symbols) as parses:
                formal.source_digest(root, self.binding())
                formal.source_digest(root, self.binding())
                self.assertEqual(2, parses.call_count)

    def test_fresh_content_same_size_mtime_and_different_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ('a', 'b')]
            for root in roots:
                root.mkdir()
                fixtures.FormalRegistryContracts().fixture(root)
            path = roots[0] / self.binding()['path']
            original = path.read_text('utf-8')
            modified = original.replace('ready', 'other')
            stamp = path.stat()
            with formal._source_digest_session() as digest:
                before = digest(roots[0], self.binding())
                path.write_text(modified, 'utf-8')
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(stamp.st_size, path.stat().st_size)
                after = digest(roots[0], self.binding())
                self.assertNotEqual(before, after)
                self.assertEqual(before, digest(roots[1], self.binding()))
                self.assertEqual(after, formal.source_digest(roots[0], self.binding()))
            with formal._source_digest_session() as digest:
                self.assertEqual(after, digest(roots[0], self.binding()))

    def test_errors_and_path_escape_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            fixtures.FormalRegistryContracts().fixture(root)
            path = root / self.binding()['path']
            original = path.read_text('utf-8')
            with formal._source_digest_session() as digest:
                digest(root, self.binding())
                path.write_text('not valid python !', 'utf-8')
                with self.assertRaises(SyntaxError):
                    digest(root, self.binding())
                path.write_text(original, 'utf-8')
                with self.assertRaises(ValueError):
                    digest(root, self.binding('absent'))
                path.unlink()
                with self.assertRaises(ValueError):
                    digest(root, self.binding())
                path.write_text(original, 'utf-8')
                with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
                    with self.assertRaises(PermissionError):
                        digest(root, self.binding())
                self.assertEqual(formal.source_digest(root, self.binding()), digest(root, self.binding()))
                for name in ('../outside.py', str(path.resolve())):
                    with self.assertRaises(ValueError):
                        digest(root, {**self.binding(), 'path': name})

    def test_symlink_retarget_is_revalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            fixtures.FormalRegistryContracts().fixture(root)
            link = root / 'link.py'
            owner = root / self.binding()['path']
            outside = Path(directory) / 'outside.py'
            outside.write_text(owner.read_text('utf-8'), 'utf-8')
            try:
                link.symlink_to(owner)
            except OSError:
                resolve = Path.resolve
                with patch.object(Path, 'resolve', lambda path: outside if path == link else resolve(path)):
                    with self.assertRaises(ValueError):
                        formal.local_file(root, 'link.py')
                return
            binding = {**self.binding(), 'path': 'link.py'}
            with formal._source_digest_session() as digest:
                digest(root, binding)
                link.unlink()
                link.symlink_to(outside)
                with self.assertRaises(ValueError):
                    digest(root, binding)

    def test_session_clears_cache_on_success_and_failure(self):
        real_cache = formal.lru_cache
        caches = []

        def tracked_cache(**kwargs):
            def wrap(function):
                cache = real_cache(**kwargs)(function)
                caches.append(cache)
                return cache
            return wrap

        with tempfile.TemporaryDirectory() as directory, patch.object(formal, 'lru_cache', tracked_cache):
            root = Path(directory)
            fixtures.FormalRegistryContracts().fixture(root)
            for fail in (False, True):
                try:
                    with formal._source_digest_session() as digest:
                        digest(root, self.binding())
                        self.assertEqual(1, caches[-1].cache_info().currsize)
                        if fail:
                            raise RuntimeError('test abort')
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertEqual(0, caches[-1].cache_info().currsize)

    def test_registry_error_lists_match_uncached_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = fixtures.FormalRegistryContracts().modeled_fixture(root)
            for mutation in ('clean', 'hash', 'cfg', 'invalid'):
                candidate = deepcopy(registry)
                if mutation == 'hash':
                    candidate['machines'][0]['source_bindings'][0]['sha256'] = '0' * 64
                elif mutation == 'cfg':
                    candidate['machines'][0]['models'][0]['invariants'] = ['Different']
                elif mutation == 'invalid':
                    candidate['machines'][0]['owners'] = ['../outside.py']
                with self.subTest(mutation=mutation):
                    expected = formal._registry_violations(root, candidate, formal.source_digest)
                    self.assertEqual(expected, formal.registry_violations(root, candidate))
                    self.assertEqual(mutation == 'clean', not expected)

    def test_public_symbol_asts_are_independent(self):
        source = 'def value():\n    return 1\n'
        first = formal.python_symbols(source)
        first['value'].name = 'changed'
        self.assertEqual('value', formal.python_symbols(source)['value'].name)

    def test_lint_rejects_cache_lifetime_and_read_bypasses(self):
        source = Path(formal.__file__).read_text('utf-8')
        self.assertEqual([], formal_parse_reuse_violations(ast.parse(source)))
        mutations = (
            ('parse_symbols.cache_clear()', 'pass'),
            ('elif not isinstance(child, ast.expr):', 'elif not isinstance(child, ast.stmt):'),
            ('if isinstance(source, ast.AST):', 'if False:'),
            ('if not isinstance(item, ast.expr):', 'if not isinstance(item, ast.stmt):'),
            ('lru_cache(maxsize=128)', 'lru_cache(maxsize=None)'),
            ("source = path.read_text('utf-8')", "source = str(path)"),
            ('symbols = parse_symbols(source)', 'symbols = parse_symbols(str(path))'),
            ('actual = reviewed_digest(root, binding)', 'actual = source_digest(root, binding)'),
            ('def python_symbols(source:', '@lru_cache(maxsize=128)\ndef python_symbols(source:'),
            ('return _registry_violations(root, registry, reviewed_digest)', 'return []'),
        )
        for old, new in mutations:
            with self.subTest(mutation=old):
                self.assertIn(old, source)
                self.assertTrue(formal_parse_reuse_violations(ast.parse(source.replace(old, new, 1))))
        self.assertTrue(formal_parse_reuse_violations(ast.parse(source + '\nsource_digest = lru_cache()(source_digest)\n')))

    def test_manifest_binds_registry_body_and_rejects_removed_hooks(self):
        repository = Path(__file__).resolve().parents[2]
        manifest = json.loads((repository / 'formal/combination-inputs-v1.json').read_text('utf-8'))
        rows = [row for row in manifest['shared_entry_points']
                if row['path'] == 'kb_platform/rag_testing/formal_state_machines.py'
                and row['symbol'] == '_registry_violations']
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(row['sha256'], formal.source_digest(repository, row))
        source = (repository / row['path']).read_text('utf-8')
        wrapper = {**row, 'symbol': 'registry_violations'}
        mutations = (
            ('errors.extend(postflight_model_violations(root, registry))', 'pass'),
            ("for binding in machine['source_bindings']:", 'for binding in []:'),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / row['path']
            target.parent.mkdir(parents=True)
            for old, new in mutations:
                with self.subTest(mutation=old):
                    self.assertIn(old, source)
                    target.write_text(source.replace(old, new, 1), 'utf-8')
                    self.assertNotEqual(row['sha256'], formal.source_digest(root, row))
                    self.assertEqual(formal.source_digest(repository, wrapper), formal.source_digest(root, wrapper))

    def test_named_declarations_match_unpruned_walk_through_all_statement_containers(self):
        source = """@decorate(lambda value: [item for item in value])
class Outer[T](factory(lambda: object)):
    if flag:
        def same(): pass
    else:
        def same(): return 2
    for item in values:
        def in_for(): pass
    else:
        def in_for_else(): pass
    while flag:
        def in_while(): pass
    else:
        def in_while_else(): pass
    with resource() as value:
        class InWith:
            def nested(self): pass
    try:
        def in_try(): pass
    except ValueError:
        def in_except(): pass
    else:
        def in_else(): pass
    finally:
        def in_finally(): pass
    try:
        def in_try_star(): pass
    except* ValueError:
        def in_except_star(): pass
    match value:
        case {'key': captured} if check(captured):
            def in_case(): pass
    def generic[U](self, value: U = factory(lambda: 1)) -> U:
        def nested(): pass
        return value
    async def asynchronous(self):
        async for item in values:
            async def in_async_for(): pass
        async with resource():
            class InAsyncWith: pass
        return lambda: [x for x in values if x]
value = (lambda item: item)([x for x in range(10)])
"""
        compile(source, 'declaration_fixture', 'exec')
        injected = ast.parse('value = call()')
        injected.body[0].value.args.append(ast.parse('def injected(): pass').body[0])
        for untrusted in (ast.parse(source), injected):
            with self.assertRaises(TypeError):
                formal.python_symbols(untrusted)
        from_bytes = formal.python_symbols(source.encode('utf-8'))
        self.assertEqual(list(formal.python_symbols(source)), list(from_bytes))
        expected = {'<module>': ast.parse(source)}

        def collect(node, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = '.'.join(filter(None, (prefix, child.name)))
                    expected[name] = child
                    collect(child, name)
                else:
                    collect(child, prefix)

        with patch.object(ast, 'iter_child_nodes', wraps=ast.iter_child_nodes) as visits:
            collect(expected['<module>'], '')
            baseline = visits.call_count
        with patch.object(ast, 'iter_child_nodes', wraps=ast.iter_child_nodes) as visits:
            actual = formal.python_symbols(source)
            self.assertLess(visits.call_count, baseline)
        self.assertEqual(list(expected), list(actual))
        self.assertGreater(len(expected), 20)
        for name in expected:
            with self.subTest(symbol=name):
                self.assertEqual(ast.dump(expected[name], include_attributes=True),
                                 ast.dump(actual[name], include_attributes=True))

        candidate_source = source.replace('pass', 'state = 0') + """
class RuntimeState: pass
def attribute():
    self.phase: str = 'open'
def subscript():
    data['status'] += 1
def chained():
    state = other = 1
def tuple_target():
    state, other = (1, 2)
def named_expression():
    return (state := 1)
def nested_expression():
    return factory(lambda: [value for value in items if (phase := value)])
"""
        expected_candidates = set()
        for name, node in formal.python_symbols(candidate_source).items():
            if isinstance(node, ast.ClassDef) and node.name.endswith(('State', 'Status', 'Phase')):
                expected_candidates.add(('fixture.py', name))
            pending = [node]
            while pending:
                item = pending.pop()
                if item is not node and isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                targets = item.targets if isinstance(item, ast.Assign) else (
                    [item.target] if isinstance(item, (ast.AnnAssign, ast.AugAssign)) else [])
                for target in targets:
                    target_name = target.attr if isinstance(target, ast.Attribute) else (
                        target.id if isinstance(target, ast.Name) else None)
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                        target_name = target.slice.value
                    if target_name in formal.STATE_NAMES:
                        expected_candidates.add(('fixture.py', name))
                pending.extend(ast.iter_child_nodes(item))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'fixture.py').write_text(candidate_source, 'utf-8')
            actual_candidates = formal.state_candidates(root, ['.'])
            self.assertEqual(sorted(expected_candidates), sorted(actual_candidates))
            for name in ('attribute', 'subscript', 'chained', 'RuntimeState'):
                self.assertIn(('fixture.py', name), actual_candidates)
            for name in ('tuple_target', 'named_expression', 'nested_expression'):
                self.assertNotIn(('fixture.py', name), actual_candidates)
            self.assertEqual(19, len(actual_candidates))

    def test_failure_only_profile_is_bounded_and_cannot_replace_required_checks(self):
        from scripts.check_project import registry_profile_workflow_violations
        from . import formal_profile
        import sys
        from types import SimpleNamespace

        check = {'name': 'Run offline quality gates',
                 'run': 'uv run --no-sync python scripts/check_project.py --output reports/ci/${{ github.run_id }}-${{ github.run_attempt }}'}
        diagnostic = {'name': 'Profile registry after Windows failure',
                      'if': "failure() && runner.os == 'Windows'", 'timeout-minutes': 3,
                      'run': 'uv run --no-sync python -m kb_platform.rag_testing.formal_profile --output reports/ci/${{ github.run_id }}-${{ github.run_attempt }}/registry-profile.json'}
        quality = {'steps': [check, diagnostic, {'name': 'Upload gate evidence', 'if': 'always()'}]}
        self.assertEqual([], registry_profile_workflow_violations(quality))
        for index, key, value in ((0, 'if', 'false'), (0, 'continue-on-error', True),
                                  (1, 'if', 'always()'), (1, 'timeout-minutes', 4),
                                  (1, 'continue-on-error', True), (1, 'run', 'echo PASS'),
                                  (2, 'if', 'success()')):
            mutant = deepcopy(quality)
            mutant['steps'][index][key] = value
            self.assertTrue(registry_profile_workflow_violations(mutant))
        mutant = deepcopy(quality)
        mutant['steps'].reverse()
        self.assertTrue(registry_profile_workflow_violations(mutant))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'diagnostic.json'
            revision = SimpleNamespace(stdout='a' * 40 + '\n' + 'b' * 40 + '\n')
            with patch.object(formal_profile.subprocess, 'run', return_value=revision), \
                    patch.object(formal_profile, 'registry_violations', return_value=[]) as registry, \
                    patch.object(sys, 'argv', ['formal_profile', '--output', str(target)]):
                self.assertEqual(0, formal_profile.main())
                registry.assert_called_once_with(Path.cwd().resolve())
            result = json.loads(target.read_text('utf-8'))
            self.assertEqual([], result['ordered_errors'])
            self.assertEqual('a' * 40, result['head'])
            self.assertEqual('b' * 40, result['tree'])
            self.assertTrue(result['profile'])
            self.assertFalse(any(Path(row['file']).is_absolute() for row in result['profile']))
            self.assertNotIn('environment', result)
            self.assertEqual('<external>/private.py', formal_profile.source_label('/private/person/private.py', root))
            self.assertNotIn('/private/person', formal_profile.safe_error('error /private/person/secret.py', root))
            self.assertNotIn('C:\\Users', formal_profile.safe_error('error C:\\Users\\person\\secret.py', root))

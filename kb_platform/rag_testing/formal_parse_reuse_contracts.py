import ast
from copy import deepcopy
import os
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

import ast
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.check_project import formal_parse_reuse_violations
from .formal_state_machines import source_digest


class WebRequestContracts(unittest.TestCase):
    def test_typescript_bindings_track_whole_modules_with_identical_checker_semantics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in ('.ts', '.tsx'):
                path = root / ('request' + suffix)
                source = b'export type Phase = "pending" | "confirmed";\n'
                path.write_bytes(source)
                binding = {'path': path.name, 'language': 'typescript-module', 'symbol': '<module>'}
                original = sha256(source).hexdigest()
                self.assertEqual(source_digest(root, binding), original)
                path.write_bytes(source.replace(b'\n', b'\r\n'))
                self.assertEqual(source_digest(root, binding), original)
                path.write_bytes(source.replace(b'confirmed', b'retry'))
                self.assertNotEqual(source_digest(root, binding), original)
                with self.assertRaisesRegex(ValueError, 'complete .ts or .tsx module'):
                    source_digest(root, {**binding, 'symbol': 'Phase'})

    def test_typescript_binding_cannot_relabel_another_language_or_escape_repository(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'request.mjs').write_text('export const request = 1;\n', encoding='utf-8')
            binding = {'path': 'request.mjs', 'language': 'typescript-module', 'symbol': '<module>'}
            with self.assertRaisesRegex(ValueError, 'complete .ts or .tsx module'):
                source_digest(root, binding)
            with self.assertRaisesRegex(ValueError, 'Repository-relative'):
                source_digest(root, {**binding, 'path': str(root / 'request.mjs')})

    def test_checker_preserves_typescript_whole_module_validation_after_hash_review(self):
        source = (Path(__file__).with_name('formal_state_machines.py')).read_text('utf-8')
        self.assertEqual(formal_parse_reuse_violations(ast.parse(source)), [])
        for old, new in (("path.suffix not in {'.ts', '.tsx'}", 'False'),
                         ("or binding['symbol'] != '<module>':", ':')):
            changed = source.replace(old, new, 1)
            self.assertNotEqual(source, changed)
            self.assertTrue(formal_parse_reuse_violations(ast.parse(changed)))

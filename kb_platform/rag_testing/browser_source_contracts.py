from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .browser_sources import ENTRIES, markup_violations, placement_violations
from .formal_state_machines import digest, source_digest, state_candidates


ROOT = Path(__file__).resolve().parents[2]


class BrowserSourceContracts(unittest.TestCase):
    def test_current_markup_has_one_explicit_mapped_owner(self):
        self.assertEqual([], placement_violations(ROOT))

    def test_html_parser_accepts_equivalent_attribute_quoting_and_nonexecuting_text(self):
        source = "<p>script onclick javascript: are plain text</p><script src='/console.mjs' type=module></script>"
        self.assertEqual([], markup_violations(source, '/console.mjs'))

    def test_markup_cannot_add_an_unmapped_execution_path(self):
        valid = '<script type="module" src="/console.mjs"></script>'
        variants = ('', valid + valid, valid + '<script src="/other.js"></script>',
                    valid.replace('</script>', 'alert(1)</script>'),
                    valid.replace('type="module"', 'type="module" type="text/javascript"'),
                    valid.replace('</script>', ''), valid.replace('></script>', '/>'),
                    valid + '<button ONCLICK="run()">Run</button>', valid + '<iframe srcdoc="x"></iframe>',
                    valid + '<a href="java&#x09;script:run()">Run</a>',
                    valid + '<object data="data:text/html,x"></object>', valid + '<base href="https://other.invalid">',
                    valid + '<meta http-equiv="refresh" content="0;url=/other">')
        for source in variants:
            with self.subTest(source=source):
                self.assertTrue(markup_violations(source, '/console.mjs'))

    def test_missing_mapped_module_is_not_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entrypoint, (_, url) = next(iter(ENTRIES.items()))
            path = root / entrypoint
            path.parent.mkdir()
            path.write_text(f'<script type="module" src="{url}"></script>', encoding='utf-8')
            self.assertTrue(placement_violations(root))

    def test_javascript_binding_is_whole_module_review_drift_not_ast_equivalence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'owner.mjs'
            source = b'let generation = 0;\nexport const next = () => ++generation;\n'
            binding = {'path': 'owner.mjs', 'language': 'javascript-module', 'symbol': '<module>'}
            path.write_bytes(source)
            original = source_digest(root, binding)
            self.assertEqual(digest(source), original)
            path.write_bytes(source.replace(b'\n', b'\r\n'))
            self.assertEqual(original, source_digest(root, binding))
            path.write_bytes(source.replace(b'++generation', b'--generation'))
            self.assertNotEqual(original, source_digest(root, binding))
            with self.assertRaisesRegex(ValueError, 'complete .mjs module'):
                source_digest(root, {**binding, 'symbol': 'next'})
            self.assertIn(('owner.mjs', '<module>'), state_candidates(root, ['.']))

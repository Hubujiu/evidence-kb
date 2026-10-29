"""A transport compatibility bridge cannot relabel old vectors with new text."""
import hashlib
import unittest

from scripts.rebuild_postgres_semantic import validate_saved_bindings


class CapsuleMigrationContracts(unittest.TestCase):
    def test_same_count_changed_text_or_source_is_rejected(self):
        current = {'source_identity': {'version': 'new'},
                   'pages': [{'page_index': 0, 'start': 0, 'end': 3, 'text': 'new'}]}
        saved = {'source': {'version': 'new'}, 'pages': [{'page_index': 0, 'start': 0, 'end': 3,
                  'text_sha256': hashlib.sha256(b'new').hexdigest()}]}
        validate_saved_bindings(saved, current)
        with self.assertRaises(ValueError):
            validate_saved_bindings({**saved, 'source': {'version': 'old'}}, current)
        with self.assertRaises(ValueError):
            validate_saved_bindings({**saved, 'pages': [{**saved['pages'][0], 'text_sha256': hashlib.sha256(b'old').hexdigest()}]}, current)

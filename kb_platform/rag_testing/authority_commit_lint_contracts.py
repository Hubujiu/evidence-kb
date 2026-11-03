from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.authority_commit_checks import authority_commit_violations


ROOT = Path(__file__).resolve().parents[2]
FILES = ('kb_platform/knowledge_sessions.py', 'kb_platform/session_store.py', 'kb_platform/knowledge_turn.py',
         'formal/session_authority/AtomicAuthorityCommit.tla')
REGISTRY = {'machines': [{'id': 'knowledge_atomic_commit'}]}


class AuthorityCommitLintContracts(unittest.TestCase):
    def test_current_commit_boundary_is_required(self):
        self.assertEqual(authority_commit_violations(ROOT, REGISTRY), [])

    def test_separate_transaction_scope_substitution_and_unknown_retry_are_rejected(self):
        mutations = (
            (FILES[0], 'if current != authority:', 'if False:'),
            (FILES[0], 'self.store.finish_in_transaction(transaction,', 'self.store.finish('),
            (FILES[0], 'expected_scope=scope', 'expected_scope=None'),
            (FILES[1], "session['scope'] != expected_scope", 'False'),
            (FILES[2], 'authority=self.authority', "authority={'scope_id': self.scope}"),
            (FILES[2], "if code == 'COMMIT_UNKNOWN':\n                raise", "if code == 'COMMIT_UNKNOWN':\n                pass"),
            (FILES[3], '/\\ (~locked \\/ ~HoldLock)', '/\\ TRUE'),
        )
        for file, before, after in mutations:
            with self.subTest(file=file, mutation=before), TemporaryDirectory() as directory:
                root = Path(directory)
                for name in FILES:
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / name).read_bytes())
                path = root / file
                original = path.read_text('utf-8')
                mutated = original.replace(before, after)
                self.assertNotEqual(original, mutated)
                path.write_text(mutated, encoding='utf-8')
                self.assertTrue(authority_commit_violations(root, REGISTRY))

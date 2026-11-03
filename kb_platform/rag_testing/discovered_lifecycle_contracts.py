from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from kb_platform.harness_sessions import MemorySession
from kb_platform.storage_adapters.sql import _SqlSpace
from kb_platform.database_contracts import DatabaseError
from kb_platform.rag_testing.discovered_lifecycle_lint import discovered_lifecycle_violations


ROOT = Path(__file__).resolve().parents[2]


class DiscoveredLifecycleContracts(unittest.TestCase):
    def test_memory_snapshots_and_both_close_paths(self):
        for close in ('finish', 'fail'):
            with self.subTest(close=close):
                message = {'text': ['original']}
                session = MemorySession(message)
                message['text'].append('external')
                snapshot = session.messages()
                session.append([{'role': 'tool', 'content': 'result'}])
                getattr(session, close)(None)
                self.assertEqual(snapshot, [{'role': 'user', 'content': {'text': ['original']}}])
                self.assertEqual(session.messages(), [])
                with self.assertRaisesRegex(RuntimeError, 'SESSION_CLOSED'):
                    session.append([{}])
                getattr(session, close)(None)
                self.assertEqual(session.messages(), [])

    def raw_space(self, *, fail=False):
        calls = []

        @contextmanager
        def transaction(**kwargs):
            calls.append(kwargs)
            if fail:
                raise RuntimeError('physical entry failed')
            yield 'physical transaction'

        physical = SimpleNamespace(identity='identity', info='info', physical_name='physical',
                                   runtime_engine=SimpleNamespace(url=SimpleNamespace(username='runtime')),
                                   transaction=transaction)
        return _SqlSpace(physical), calls

    def test_raw_transaction_seals_even_when_physical_entry_fails(self):
        for fail in (False, True):
            space, calls = self.raw_space(fail=fail)
            try:
                with space.transaction(mode='read', operation_id='operation') as tx:
                    self.assertEqual(tx, 'physical transaction')
            except RuntimeError:
                self.assertTrue(fail)
            self.assertEqual(calls, [{'write': False, 'limits': None, 'operation_id': 'operation'}])
            self.assertTrue(space._sealed)
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_PREPARE_CLOSED'):
                space.prepare(None)

    def test_invalid_mode_never_seals_or_enters_physical_transaction(self):
        for kwargs in ({'mode': 'invalid'}, {'write': 1}):
            space, calls = self.raw_space()
            with self.assertRaisesRegex(ValueError, 'INVALID_DATABASE_TRANSACTION_MODE'):
                with space.transaction(**kwargs):
                    self.fail('invalid transaction entered')
            self.assertFalse(space._sealed)
            self.assertEqual(calls, [])

    def test_reviewed_sources_and_semantic_mutants(self):
        self.assertEqual(discovered_lifecycle_violations(ROOT), [])
        mutants = {
            'kb_platform/harness_sessions.py': [
                ('self._records.extend(deepcopy(records))', 'self._records.extend(records)'),
                ('return deepcopy(self._records)', 'return self._records'),
                ('self._records.clear()', 'pass')],
            'kb_platform/storage_adapters/sql.py': [
                ('if self._sealed or not isinstance(package, SchemaPackage):', 'if not isinstance(package, SchemaPackage):'),
                ('self._sealed = True\n        with self._space.transaction', 'self._sealed = False\n        with self._space.transaction')],
            'scripts/delivery_control.py': [
                ("published = True\n        os.write(write_fd, b'1')", "os.write(write_fd, b'1')\n        published = True"),
                ("actual_start != receipt['process_start'] or executable", "False or executable"),
                ('stat.S_ISREG(marker_info.st_mode)', 'True'),
                ("marker.name != 'stop.request'", 'False'),
                ('if published:', 'if False:'),
                ("response.headers.get('X-Delivery-Instance') == instance", 'True')],
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path, pairs in mutants.items():
                source = (ROOT / path).read_text('utf-8')
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                for before, after in pairs:
                    with self.subTest(path=path, before=before):
                        # Replace the last duplicate to exercise MemorySession rather than DurableSession.
                        head, separator, tail = source.rpartition(before)
                        self.assertTrue(separator)
                        target.write_text(head + after + tail, encoding='utf-8')
                        self.assertTrue(discovered_lifecycle_violations(root))
                target.unlink()

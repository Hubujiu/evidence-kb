from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .budget import BudgetLedger
from .embedding_jobs_contracts import google_audit, google_preflight_fixture


class LedgerRaceContracts(unittest.TestCase):
    def test_first_open_conflicting_caps_are_serialized_before_acceptance(self):
        checked, probed, committed = Event(), Event(), Event()
        real_connect = sqlite3.connect
        results = {}

        class Connection(sqlite3.Connection):
            def execute(inner, query, parameters=()):
                name = current_thread().name
                if name == 'larger-caps' and query == 'BEGIN IMMEDIATE':
                    probed.set()
                result = super().execute(query, parameters)
                if query == 'SELECT caps FROM config WHERE id=1':
                    row = result.fetchone()
                    if name == 'smaller-caps':
                        checked.set()
                        if not probed.wait(3):
                            raise RuntimeError('second constructor did not reach admission')
                    elif row is None:
                        probed.set()
                        if not committed.wait(3):
                            raise RuntimeError('first constructor did not commit')
                    return SimpleNamespace(fetchone=lambda: row)
                return result

        def connect(*args, **kwargs):
            return real_connect(*args, factory=Connection, **kwargs)

        with TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.sqlite'

            def construct(name, cap):
                try:
                    results[name] = BudgetLedger(path, {'calls': cap})
                except BaseException as error:
                    results[name] = error
                finally:
                    if name == 'small':
                        committed.set()

            with patch('kb_platform.rag_testing.budget.sqlite3.connect', side_effect=connect):
                small = Thread(target=construct, args=('small', 1), name='smaller-caps')
                large = Thread(target=construct, args=('large', 100), name='larger-caps')
                small.start()
                self.assertTrue(checked.wait(3))
                large.start()
                small.join(5)
                large.join(5)
            self.assertFalse(small.is_alive())
            self.assertFalse(large.is_alive())
            self.assertIsInstance(results['small'], BudgetLedger)
            self.assertIsInstance(results['large'], ValueError)
            with closing(real_connect(path)) as database:
                self.assertEqual(json.loads(database.execute('SELECT caps FROM config WHERE id=1').fetchone()[0]),
                                 {'calls': 1})

    def test_constructor_detaches_caps_from_caller_mutation(self):
        with TemporaryDirectory() as directory:
            caps = {'calls': 1}
            ledger = BudgetLedger(Path(directory) / 'budget.sqlite', caps)
            caps['calls'] = 100
            with self.assertRaisesRegex(ValueError, 'BUDGET_INSUFFICIENT'):
                ledger.reserve('two', {'calls': 2})

    def test_google_unknown_between_precheck_and_reservation_blocks_send(self):
        with google_preflight_fixture() as provider:
            provider.ledger = BudgetLedger(provider.ledger.path.with_name('concurrent.sqlite'),
                {'remote_embedding_calls': 2, 'input_tokens': 16384})
            provider.remote.platform, provider.remote._headers, provider.remote._issued_requests = 'gemini_api', {}, 0
            provider.remote._validated_vector = lambda values: values

            def respond(*args):
                provider.remote.calls += 1
                provider.remote._issued_requests += 1
                return {'embedding': {'values': [1., 0.]}, 'usageMetadata': {'promptTokenCount': 1}}

            provider.remote._post = respond
            before = provider.ledger.summary

            def race():
                snapshot = before()
                provider.ledger.reserve('competing', {'remote_embedding_calls': 1, 'input_tokens': 8192})
                provider.ledger.finish('competing', status='UNKNOWN')
                return snapshot

            with patch.object(provider.ledger, 'summary', side_effect=race):
                with self.assertRaisesRegex(Exception, 'UNRESOLVED_PHYSICAL_REQUEST'):
                    provider.documents(['original'], request_id='blocked', audit_context=google_audit(),
                                       authorize=lambda: True)
            self.assertEqual(provider.remote.calls, 0)
            self.assertEqual(before()['physical_requests'], 1)
            self.assertEqual(before()['pending_or_unknown'], 1)


if __name__ == '__main__':
    unittest.main()

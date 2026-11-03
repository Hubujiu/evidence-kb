from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from kb_platform.database_contracts import DatabaseError
from kb_platform.persistence_schema import budget_schema
from .storage_structured_wire_contracts import structured


class StorageHandleGuardContracts(unittest.TestCase):
    def test_failed_heartbeat_start_releases_actual_guard_before_preserving_original_error(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            original, failure, bodies, joins = Thread.start, RuntimeError('synthetic heartbeat allocation failure'), [], []
            original_join = Thread.join

            def start(thread):
                if thread.name == 'storage-guard-heartbeat':
                    raise failure
                return original(thread)

            def join(thread, *args, **kwargs):
                if thread.name == 'storage-guard-heartbeat':
                    joins.append(thread.ident)
                return original_join(thread, *args, **kwargs)

            with patch.object(Thread, 'start', start), patch.object(Thread, 'join', join):
                with self.assertRaises(RuntimeError) as caught:
                    with client.guard('producer'):
                        bodies.append(True)
            self.assertIs(caught.exception, failure)
            self.assertEqual(joins, [])
            self.assertEqual(bodies, [])
            self.assertEqual(endpoint._handles, {})
            self.assertEqual([value['status'] for value in endpoint._outcomes.values()], ['RELEASED'])
            with client.guard('producer'):
                self.assertEqual(len(endpoint._handles), 1)
            self.assertEqual(endpoint._handles, {})

    def test_failed_heartbeat_start_preserves_original_error_if_release_receipt_is_lost(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            original, send = Thread.start, client._send
            failure, finishes = RuntimeError('synthetic heartbeat allocation failure'), []

            def start(thread):
                if thread.name == 'storage-guard-heartbeat':
                    raise failure
                return original(thread)

            def lose_release(request):
                result = send(request)
                if request['op'] == 'finish':
                    finishes.append(result)
                    raise OSError('synthetic lost release receipt')
                return result

            with patch.object(Thread, 'start', start), patch.object(client, '_send', lose_release):
                with self.assertRaises(RuntimeError) as caught:
                    with client.guard('producer'):
                        self.fail('heartbeat startup failed')
            self.assertIs(caught.exception, failure)
            self.assertEqual(finishes, [{'status': 'RELEASED', 'operation_id': None}])
            self.assertEqual(endpoint._handles, {})
            with client.guard('producer'):
                self.assertEqual(len(endpoint._handles), 1)

    def test_outcome_cache_evicts_only_oldest_without_replaying_or_fabricating_finish(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            tokens = []
            with patch.object(endpoint._store, 'transaction', wraps=endpoint._store.transaction) as factory:
                for index in range(257):
                    with client.transaction(write=False, operation_id=f'cache-{index}') as tx:
                        tokens.append(tx._token)
                self.assertEqual(len(endpoint._outcomes), 256)
                self.assertNotIn(tokens[0], endpoint._outcomes)
                self.assertEqual(list(endpoint._outcomes), tokens[1:])
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_CLOSED'):
                    endpoint.execute({'version': 1, 'op': 'finish', 'handle': tokens[0], 'commit': True})
                for token, index in ((tokens[1], 1), (tokens[-1], 256)):
                    self.assertEqual(endpoint.execute({'version': 1, 'op': 'finish', 'handle': token, 'commit': False}),
                        {'status': 'COMMITTED', 'operation_id': f'cache-{index}'})
                self.assertEqual(factory.call_count, 257)
            self.assertEqual(endpoint._handles, {})

    def test_guard_ping_renews_idle_budget_and_expiry_releases_real_sqlite_exclusion(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            expire, first_wait, waits, clock = Event(), Event(), [], [0.0]

            class LeaseQueue(Queue):
                def get(self, block=True, timeout=None):
                    if block and timeout is not None:
                        waits.append(timeout)
                        if len(waits) == 1:
                            first_wait.set()
                            return super().get()
                        if not expire.wait(5):
                            raise AssertionError('expiry barrier not released')
                        raise Empty
                    return super().get(block, timeout)

            with patch('kb_platform.storage_structured_wire.Queue', LeaseQueue), patch(
                    'kb_platform.storage_structured_wire.monotonic', side_effect=lambda: clock[0]):
                opened = client._call({'op': 'guard', 'name': 'leased', 'nowait': True})
                handle = endpoint._handles[opened['handle']]
                try:
                    self.assertTrue(first_wait.wait(5))
                    clock[0] = 10.0
                    self.assertEqual(client._call({'op': 'ping', 'handle': handle.id}), {'status': 'HELD'})
                    with self.assertRaisesRegex(DatabaseError, 'DATABASE_GUARD_BUSY'):
                        with endpoint._store.guard('leased'):
                            self.fail('actual guard exclusion was lost')
                finally:
                    expire.set()
                    handle._thread.join(5)
                self.assertFalse(handle._thread.is_alive())
                self.assertEqual(waits, [60.0, 60.0])
                self.assertEqual(endpoint._outcomes[handle.id], {'status': 'ROLLED_BACK', 'operation_id': None})
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_CLOSED'):
                    client._call({'op': 'ping', 'handle': handle.id})
                with endpoint._store.guard('leased'):
                    self.assertEqual(endpoint._handles, {})

    def test_handle_capacity_rejects_before_factory_without_retiring_live_transactions(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            with patch('kb_platform.storage_structured_wire.MAX_HANDLES', 2), patch.object(
                    endpoint._store, 'transaction', wraps=endpoint._store.transaction) as factory:
                with client.transaction(write=False) as first, client.transaction(write=False) as second:
                    self.assertEqual(len(endpoint._handles), 2)
                    with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_LIMIT'):
                        with client.transaction(write=False):
                            self.fail('capacity rejection must precede body')
                    self.assertEqual(factory.call_count, 2)
                    self.assertEqual(set(endpoint._handles), {first._token, second._token})
                    self.assertTrue(all(not handle._stopped.is_set() for handle in endpoint._handles.values()))
                with client.transaction(write=False):
                    self.assertEqual(factory.call_count, 3)
            self.assertEqual(endpoint._handles, {})

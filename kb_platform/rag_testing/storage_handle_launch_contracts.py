from contextlib import contextmanager
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from sqlalchemy import select

from kb_platform.database_contracts import DatabaseError
from kb_platform.persistence_schema import budget_calls, budget_schema
from kb_platform.storage_structured_wire import _Handle
from .storage_structured_wire_contracts import structured


def row(identity):
    return {'id': identity, 'status': 'UNKNOWN', 'reserved': '{}', 'usage': None,
            'category': 'synthetic', 'diagnostic': None}


class StorageHandleLaunchContracts(unittest.TestCase):
    def test_failed_native_start_frees_slot_preserves_exception_and_allows_fresh_write(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            failure = RuntimeError('synthetic native thread allocation failure')
            with patch('kb_platform.storage_structured_wire.Thread.start', side_effect=failure):
                with self.assertRaises(RuntimeError) as caught:
                    with client.transaction(operation_id='unstarted'):
                        self.fail('unstarted transaction body ran')
            self.assertIs(caught.exception, failure)
            self.assertEqual(endpoint._handles, {})
            self.assertEqual(list(endpoint._outcomes.values()), [{'status': 'UNKNOWN', 'operation_id': 'unstarted'}])
            with client.transaction(operation_id='fresh') as tx:
                tx.insert(budget_calls, row('fresh'))
            with client.transaction(write=False) as tx:
                self.assertEqual([item['id'] for item in tx.execute(select(budget_calls)).rows], ['fresh'])

    def test_failed_launch_does_not_consume_capacity_or_retire_an_existing_writer(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            with patch('kb_platform.storage_structured_wire.MAX_HANDLES', 2):
                with client.transaction(operation_id='live') as tx:
                    live = next(iter(endpoint._handles.values()))
                    for index in range(3):
                        with patch('kb_platform.storage_structured_wire.Thread.start',
                                   side_effect=RuntimeError('synthetic allocation failure')):
                            with self.assertRaises(RuntimeError):
                                with client.transaction(operation_id=f'unstarted-{index}'):
                                    self.fail('unstarted body ran')
                        self.assertEqual(list(endpoint._handles.values()), [live])
                        self.assertFalse(live._stopped.is_set())
                    tx.insert(budget_calls, row('live-write'))
            with client.transaction(write=False) as tx:
                self.assertEqual([item['id'] for item in tx.execute(select(budget_calls)).rows], ['live-write'])

    def test_close_before_start_is_idempotent_and_never_starts_or_joins_a_thread(self):
        entered, finished = [], []

        @contextmanager
        def manager():
            entered.append(True)
            yield None

        handle = _Handle(manager, kind='transaction', seconds=1, operation_id='closed-before-start',
                         finished=lambda *args: finished.append(args))
        with patch.object(handle._thread, 'start') as start, patch.object(handle._thread, 'join') as join:
            handle.close()
            handle.close()
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_CLOSED'):
                handle.start()
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_CLOSED'):
                handle.call('commit')
            start.assert_not_called()
            join.assert_not_called()
        self.assertEqual(entered, [])
        self.assertEqual(finished, [(handle.id, {'status': 'UNKNOWN', 'operation_id': 'closed-before-start'})])

    def test_public_close_wins_published_handle_before_native_launch(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            paused, resume, errors, body = Event(), Event(), [], []
            original = _Handle.start

            def start(handle):
                paused.set()
                if not resume.wait(5):
                    raise AssertionError('test launch barrier was not released')
                return original(handle)

            def begin():
                try:
                    with client.transaction(operation_id='close-before-launch'):
                        body.append(True)
                except BaseException as error:
                    errors.append(error)

            with patch.object(_Handle, 'start', start), patch.object(endpoint._store, 'transaction',
                    wraps=endpoint._store.transaction) as factory:
                producer = Thread(target=begin)
                producer.start()
                try:
                    self.assertTrue(paused.wait(5))
                    self.assertEqual(len(endpoint._handles), 1)
                    endpoint.close()
                finally:
                    resume.set()
                    producer.join(5)
                self.assertFalse(producer.is_alive())
                self.assertEqual(body, [])
                self.assertEqual([getattr(error, 'code', None) for error in errors], ['STORAGE_HANDLE_CLOSED'])
                self.assertEqual(endpoint._handles, {})
                factory.assert_not_called()

    def test_second_start_cannot_retire_a_running_handle(self):
        entered, finished = [], []

        @contextmanager
        def manager():
            entered.append(True)
            yield None

        handle = _Handle(manager, kind='transaction', seconds=5, operation_id='one-start',
                         finished=lambda *args: finished.append(args))
        handle.start()
        try:
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_BUSY'):
                handle.start()
            self.assertFalse(handle._stopped.is_set())
            self.assertEqual(finished, [])
            self.assertEqual(handle.call(lambda resource: {'usable': True}), {'usable': True})
        finally:
            handle.close()
        self.assertEqual(entered, [True])
        self.assertEqual(finished, [(handle.id, {'status': 'ROLLED_BACK', 'operation_id': 'one-start'})])

    def test_ready_timeout_does_not_free_a_launched_worker_before_real_cleanup(self):
        entering, resume, stopped, finished = Event(), Event(), Event(), []

        @contextmanager
        def manager():
            entering.set()
            if not resume.wait(5):
                raise AssertionError('test resource barrier was not released')
            yield None

        def terminal(*args):
            finished.append(args)
            stopped.set()

        handle = _Handle(manager, kind='transaction', seconds=.01, operation_id='delayed-enter', finished=terminal)
        try:
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_TIMEOUT'):
                handle.start()
            self.assertTrue(entering.is_set())
            self.assertTrue(handle._thread.is_alive())
            self.assertEqual(finished, [])
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_BUSY'):
                handle.close()
            self.assertEqual(finished, [])
        finally:
            resume.set()
            self.assertTrue(stopped.wait(5))
            handle.close()
        self.assertEqual(finished, [(handle.id, {'status': 'ROLLED_BACK', 'operation_id': 'delayed-enter'})])

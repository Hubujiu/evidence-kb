from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from sqlalchemy import select

from kb_platform.database_contracts import DatabaseError
from kb_platform.persistence_schema import budget_calls, budget_schema
from kb_platform.storage_structured_wire import _Handle
from .storage_handle_launch_contracts import row
from .storage_structured_wire_contracts import structured


class StorageHandleQueueContracts(unittest.TestCase):
    def test_timeout_cancellation_during_drain_cannot_lose_terminal_ownership(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            original = endpoint._store.transaction
            entered, release, drain, cancelled = Event(), Event(), Event(), Event()
            worker_errors, callers, cancellations, receipts, executed = [], [], [], [], []

            @contextmanager
            def fail_after_enter(**options):
                with original(**options) as transaction:
                    transaction.insert(budget_calls, row('rolled-back'))
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('enter barrier not released')
                    raise ValueError('synthetic failure after actual SQLite insert')
                    yield transaction

            class DrainFuture(Future):
                def result(self, timeout=None):
                    if not drain.wait(5):
                        raise AssertionError('drain barrier not reached')
                    raise TimeoutError()

                def cancel(self):
                    answer = super().cancel()
                    cancellations.append(answer)
                    cancelled.set()
                    return answer

                def set_exception(self, error):
                    if getattr(error, 'code', None) == 'STORAGE_HANDLE_CLOSED':
                        drain.set()
                        if not cancelled.wait(5):
                            raise AssertionError('timeout cancellation not reached')
                    return super().set_exception(error)

            def begin():
                try:
                    with client.transaction(operation_id='drain-race'):
                        self.fail('failed manager entered user body')
                except BaseException as error:
                    callers.append(error)

            def queued_call(handle):
                try:
                    handle.call(lambda resource: executed.append(True))
                except BaseException as error:
                    receipts.append(error)

            with patch.object(endpoint._store, 'transaction', fail_after_enter), patch(
                    'threading.excepthook', side_effect=lambda args: worker_errors.append(args.exc_value)):
                opening = Thread(target=begin)
                opening.start()
                queued = None
                try:
                    self.assertTrue(entered.wait(5))
                    handle = next(iter(endpoint._handles.values()))
                    pending = DrainFuture()
                    with patch('kb_platform.storage_structured_wire.Future', return_value=pending):
                        queued = Thread(target=queued_call, args=(handle,))
                        queued.start()
                        self.assertTrue(self.wait_for_queue(handle, pending))
                        release.set()
                        queued.join(5)
                    opening.join(5)
                    handle._thread.join(5)
                    self.assertFalse(queued.is_alive())
                    self.assertFalse(opening.is_alive())
                    self.assertFalse(handle._thread.is_alive())
                finally:
                    release.set()
                    if queued is not None:
                        queued.join(5)
                    opening.join(5)
            self.assertEqual(worker_errors, [])
            self.assertEqual(cancellations, [False])
            self.assertEqual([getattr(error, 'code', None) for error in receipts], ['STORAGE_HANDLE_TIMEOUT'])
            self.assertEqual([str(error) for error in callers], ['synthetic failure after actual SQLite insert'])
            self.assertEqual(executed, [])
            self.assertEqual(endpoint._handles, {})
            self.assertEqual(endpoint._outcomes[handle.id], {'status': 'UNKNOWN', 'operation_id': 'drain-race'})
            with client.transaction(write=False) as tx:
                self.assertEqual(tx.execute(select(budget_calls)).rows, ())

    def wait_for_queue(self, handle, pending):
        with handle._queue.mutex:
            if any(item[1] is pending for item in handle._queue.queue):
                return True
        with handle._queue.not_empty:
            return handle._queue.not_empty.wait_for(
                lambda: any(item[1] is pending for item in handle._queue.queue), timeout=5)

    def test_cancelled_queued_action_never_executes_and_terminal_receipt_survives(self):
        finished, executed = [], []

        @contextmanager
        def manager():
            yield None

        handle = _Handle(manager, kind='transaction', seconds=5, operation_id='cancelled',
                         finished=lambda *args: finished.append(args))
        cancelled, terminal, residual = Future(), Future(), Future()
        self.assertTrue(cancelled.cancel())
        self.assertTrue(residual.cancel())
        handle._queue.put_nowait((lambda resource: executed.append(True), cancelled))
        handle._queue.put_nowait(('rollback', terminal))
        handle._queue.put_nowait((lambda resource: executed.append(True), residual))
        handle.start()
        self.assertEqual(terminal.result(5), {'status': 'ROLLED_BACK', 'operation_id': 'cancelled'})
        handle.close()
        self.assertEqual(executed, [])
        self.assertTrue(cancelled.cancelled())
        self.assertTrue(residual.cancelled())
        self.assertEqual(finished, [(handle.id, {'status': 'ROLLED_BACK', 'operation_id': 'cancelled'})])

    def test_full_queue_rejects_without_stopping_live_worker(self):
        entered, release, finished = Event(), Event(), []

        @contextmanager
        def manager():
            entered.set()
            if not release.wait(5):
                raise AssertionError('capacity barrier not released')
            yield None

        handle = _Handle(manager, kind='transaction', seconds=10, operation_id='queue-capacity',
                         finished=lambda *args: finished.append(args))
        starter = Thread(target=handle.start)
        starter.start()
        try:
            self.assertTrue(entered.wait(5))
            waiting = [Future() for _ in range(64)]
            for pending in waiting:
                handle._queue.put_nowait((lambda resource: {'ok': True}, pending))
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_HANDLE_BUSY'):
                handle.call(lambda resource: None)
            self.assertFalse(handle._stopped.is_set())
            release.set()
            starter.join(5)
            self.assertFalse(starter.is_alive())
            for pending in waiting:
                self.assertEqual(pending.result(5), {'ok': True})
        finally:
            release.set()
            starter.join(5)
            handle.close()
        self.assertEqual(finished, [(handle.id, {'status': 'ROLLED_BACK', 'operation_id': 'queue-capacity'})])

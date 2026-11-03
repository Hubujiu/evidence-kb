from io import BytesIO
import json
from threading import Event, Lock, Thread
import unittest

from kb_platform.host_protocol import HostConnection, HostProtocolError, PROTOCOL
from kb_platform.host_worker import ApplicationWorker


class HostOwnerContracts(unittest.TestCase):
    def test_unique_ids_can_arrive_two_then_one_and_correlate_exactly(self):
        first_allocated, first_sent, second_sent, release_first = Event(), Event(), Event(), Event()
        output, results, errors = BytesIO(), {}, []
        connection = HostConnection(output)
        send = connection.send

        def ordered(frame):
            if frame['id'] == 1:
                first_allocated.set()
                if not release_first.wait(3):
                    raise RuntimeError('FIRST_SEND_BARRIER_EXPIRED')
            send(frame)
            (first_sent if frame['id'] == 1 else second_sent).set()

        def request(label):
            try:
                results[label] = connection.request('native', {'label': label})
            except BaseException as error:
                errors.append(error)

        connection.send = ordered
        first, second = Thread(target=request, args=('first',)), Thread(target=request, args=('second',))
        first.start()
        try:
            self.assertTrue(first_allocated.wait(3))
            second.start()
            self.assertTrue(second_sent.wait(3))
            connection.receive({'id': 2, 'ok': True, 'result': 'two'})
            second.join(3)
            self.assertFalse(second.is_alive())
            release_first.set()
            self.assertTrue(first_sent.wait(3))
            connection.receive({'id': 1, 'ok': True, 'result': 'one'})
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertEqual([json.loads(line)['id'] for line in output.getvalue().splitlines()], [2, 1])
            self.assertEqual(results, {'first': 'one', 'second': 'two'})
            self.assertEqual(errors, [])
            self.assertEqual(connection._pending, {})
            with self.assertRaises(HostProtocolError) as error:
                connection.receive({'id': 1, 'ok': True, 'result': 'duplicate'})
            self.assertEqual(error.exception.code, 'HOST_UNKNOWN_RESPONSE')
            connection.fail(OSError('closed'))
            with self.assertRaises(HostProtocolError) as error:
                connection.request('native', {})
            self.assertEqual(error.exception.code, 'HOST_CONNECTION_CLOSED')
        finally:
            release_first.set()
            connection.fail(OSError('test cleanup'))
            first.join(3)
            if second.ident is not None:
                second.join(3)

    def test_shutdown_waits_for_stop_and_real_close_before_reply(self):
        run_started, stop_started, stop_release, run_returned = Event(), Event(), Event(), Event()
        wait_started, close_started, close_release = Event(), Event(), Event()
        run_replied, shutdown_replied = Event(), Event()
        log, frames, lock = [], [], Lock()

        class TracedEvent(Event):
            def wait(self, timeout=None):
                wait_started.set()
                return super().wait(timeout)

        class App:
            def run(self):
                log.append('run.start')
                run_started.set()
                if not stop_started.wait(3):
                    raise RuntimeError('STOP_NOT_STARTED')
                log.append('run.return')
                run_returned.set()
                return 'value'

            def stop(self):
                log.append('stop.start')
                stop_started.set()
                if not stop_release.wait(3):
                    raise RuntimeError('STOP_BARRIER_EXPIRED')
                log.append('stop.end')

            def close(self):
                log.append('close.start')
                close_started.set()
                if not close_release.wait(3):
                    raise RuntimeError('CLOSE_BARRIER_EXPIRED')
                log.append('close.end')

        class Connection:
            def reply(self, identity, result=None, error=None):
                with lock:
                    frames.append((identity, result, error))
                    log.append(f'reply.{identity}')
                (run_replied if identity == 3 else shutdown_replied).set()

            def fail(self, error):
                frames.append(('failure', None, error))

        worker = ApplicationWorker(Connection())
        worker._phase, worker._application = 'ready', App()
        worker._stop_done = TracedEvent()
        worker._stop_done.set()
        try:
            worker.accept({'protocol': PROTOCOL, 'kind': 'request', 'id': 3, 'method': 'run', 'params': {}})
            self.assertTrue(run_started.wait(3))
            worker.accept({'protocol': PROTOCOL, 'kind': 'request', 'id': 4, 'method': 'shutdown', 'params': {}})
            self.assertTrue(stop_started.wait(3) and run_returned.wait(3) and wait_started.wait(3))
            self.assertFalse(close_started.is_set() or shutdown_replied.is_set())
            stop_release.set()
            self.assertTrue(close_started.wait(3))
            self.assertFalse(shutdown_replied.is_set() or worker._settled.is_set())
            close_release.set()
            self.assertTrue(run_replied.wait(3) and shutdown_replied.wait(3) and worker.stopped.wait(3))
            self.assertEqual(frames, [(3, 'value', None), (4, {'stopped': True}, None)])
            self.assertLess(log.index('stop.end'), log.index('close.start'))
            self.assertLess(log.index('close.end'), log.index('reply.3'))
            self.assertLess(log.index('reply.3'), log.index('reply.4'))
        finally:
            stop_release.set()
            close_release.set()
            worker._settled.wait(3)
            worker.stopped.wait(3)

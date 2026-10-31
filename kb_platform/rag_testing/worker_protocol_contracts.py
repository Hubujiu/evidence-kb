import io
import json
from threading import Event, Lock, Thread
import unittest

from kb_platform.host_protocol import HostConnection, HostProtocolError, PROTOCOL
from kb_platform.host_worker import ApplicationWorker, receive_messages


class Replies:
    def __init__(self):
        self.rows = []
        self.run_response = Event()
        self.release_run_response = Event()

    def reply(self, identity, result=None, error=None):
        self.rows.append((identity, result, error))
        if identity == 3:
            self.run_response.set()
            if not self.release_run_response.wait(5):
                raise RuntimeError('test did not release run response')

    def fail(self, error):
        self.rows.append(('transport', None, error))


class ObservedEvent:
    def __init__(self):
        self.event = Event()
        self.waiting = Event()

    def set(self):
        self.event.set()

    def clear(self):
        self.event.clear()

    def is_set(self):
        return self.event.is_set()

    def wait(self):
        self.waiting.set()
        return self.event.wait(5)


class RecordingWriter(io.BytesIO):
    def __init__(self, expected):
        super().__init__()
        self.expected = expected
        self.frames = []
        self.sent = Event()

    def write(self, payload):
        self.frames.append(json.loads(payload))
        result = super().write(payload)
        if len(self.frames) == self.expected:
            self.sent.set()
        return result


class WorkerProtocolContracts(unittest.TestCase):
    def test_host_eof_after_shutdown_delivery_waits_for_response_flush(self):
        delivered, release_flush = Event(), Event()

        class Writer(io.BytesIO):
            def flush(self):
                delivered.set()
                if not release_flush.wait(5):
                    raise OSError('test did not release shutdown flush')

        connection = HostConnection(Writer())
        worker = ApplicationWorker(connection)
        worker.stopped = ObservedEvent()
        worker._phase = 'settled'
        receiver = Thread(target=receive_messages, args=(io.BytesIO(b''), connection, worker))
        worker.accept({'id': 4, 'method': 'shutdown', 'params': {}})
        try:
            self.assertTrue(delivered.wait(5))
            receiver.start()
            self.assertTrue(worker.stopped.waiting.wait(5))
            self.assertIsNone(connection._failure)
            self.assertFalse(worker.stopped.is_set())
        finally:
            release_flush.set()
            if receiver.ident is not None:
                receiver.join(5)
        self.assertFalse(receiver.is_alive())
        self.assertTrue(worker.stopped.is_set())
        self.assertIsNone(connection._failure)
        self.assertEqual({'stopped': True}, json.loads(connection.writer.getvalue())['result'])

    def test_host_eof_before_shutdown_response_remains_transport_failure(self):
        connection = HostConnection(io.BytesIO())
        worker = ApplicationWorker(connection)
        receive_messages(io.BytesIO(b''), connection, worker)
        self.assertIsInstance(connection._failure, EOFError)
        self.assertTrue(worker.stopped.is_set())

    def test_malformed_frame_during_shutdown_remains_protocol_failure(self):
        connection = HostConnection(io.BytesIO())
        worker = ApplicationWorker(connection)
        worker._shutdown_responding.set()
        receive_messages(io.BytesIO(b'not-json\n'), connection, worker)
        self.assertIsInstance(connection._failure, HostProtocolError)
        self.assertEqual('HOST_INVALID_JSON', connection._failure.code)
        self.assertTrue(worker.stopped.is_set())

    def test_shutdown_after_run_response_does_not_stop_closed_application(self):
        calls = []

        class Application:
            def run(self):
                calls.append('run')
                return 'done'

            def close(self):
                calls.append('close')

            def stop(self):
                calls.append('stop')
                raise RuntimeError('stop called after application close')

        connection = Replies()
        worker = ApplicationWorker(connection)
        worker._phase, worker._application = 'ready', Application()
        worker.accept({'id': 3, 'method': 'run', 'params': {}})
        try:
            self.assertTrue(connection.run_response.wait(5))
            worker.accept({'id': 4, 'method': 'shutdown', 'params': {}})
        finally:
            connection.release_run_response.set()
        self.assertTrue(worker.stopped.wait(5))
        self.assertEqual(['run', 'close'], calls)
        self.assertEqual([(3, 'done', None), (4, {'stopped': True}, None)], connection.rows)

    def test_cleanup_waits_for_in_flight_stop_callback(self):
        calls = []
        running, release_run, stopping, release_stop, closed = (Event() for _ in range(5))

        class Application:
            def run(self):
                running.set()
                if not release_run.wait(5):
                    raise RuntimeError('test did not release run')
                return 'done'

            def stop(self):
                calls.append('stop entered')
                stopping.set()
                release_run.set()
                if not release_stop.wait(5):
                    raise RuntimeError('test did not release stop')
                calls.append('stop returned')

            def close(self):
                calls.append('close')
                closed.set()

        connection = Replies()
        connection.release_run_response.set()
        worker = ApplicationWorker(connection)
        worker._stop_done = ObservedEvent()
        worker._stop_done.set()
        worker._phase, worker._application = 'ready', Application()
        worker.accept({'id': 3, 'method': 'run', 'params': {}})
        try:
            self.assertTrue(running.wait(5))
            worker.accept({'id': 4, 'method': 'shutdown', 'params': {}})
            self.assertTrue(stopping.wait(5))
            self.assertTrue(worker._stop_done.waiting.wait(5))
            self.assertFalse(closed.is_set())
        finally:
            release_run.set()
            release_stop.set()
        self.assertTrue(worker.stopped.wait(5))
        self.assertEqual(['stop entered', 'stop returned', 'close'], calls)
        self.assertTrue(all(error is None for _, _, error in connection.rows))

    def test_out_of_order_responses_complete_their_own_concurrent_requests(self):
        writer = RecordingWriter(2)
        connection = HostConnection(writer)
        results, errors, lock = {}, [], Lock()

        def request(name):
            try:
                result = connection.request('native', {'name': name})
                with lock:
                    results[name] = result
            except BaseException as exc:
                errors.append(exc)

        threads = [Thread(target=request, args=(name,)) for name in ('first', 'second')]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(writer.sent.wait(5))
            for frame in reversed(writer.frames):
                connection.receive({'protocol': PROTOCOL, 'kind': 'response', 'id': frame['id'],
                                    'ok': True, 'result': frame['params']['name']})
        finally:
            connection.fail(OSError('test finished'))
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual({'first': 'first', 'second': 'second'}, results)

    def test_transport_failure_releases_waiting_request_and_rejects_new_calls(self):
        writer = RecordingWriter(1)
        connection = HostConnection(writer)
        errors = []

        def request():
            try:
                connection.request('native', {})
            except HostProtocolError as exc:
                errors.append(exc.code)

        thread = Thread(target=request)
        thread.start()
        try:
            self.assertTrue(writer.sent.wait(5))
        finally:
            connection.fail(EOFError('host exited'))
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(['HOST_CONNECTION_CLOSED'], errors)
        with self.assertRaises(HostProtocolError):
            connection.request('native', {})

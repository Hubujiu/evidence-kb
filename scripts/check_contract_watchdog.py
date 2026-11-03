import json
import os
import sys
import threading
import time


def valid_capture(status, required):
    return bool(status['thread_stopped'] and status['error'] is None
                and (not (required or status['due'] or status['triggered']) or status['written']))


class SnapshotWatchdog:
    def __init__(self, timeout, fd):
        self._stop = threading.Event()
        self._fd = fd
        self.triggered = False
        self.written = False
        self.error = None
        self._deadline = time.monotonic() + timeout
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        if self._stop.wait(max(0, self._deadline - time.monotonic())) or self._stop.is_set():
            return
        self.triggered = True
        try:
            data = []
            all_frames = sys._current_frames()
            for thread_id, frame in list(all_frames.items())[:128]:
                stack = []
                while frame is not None and len(stack) < 64:
                    code = frame.f_code
                    stack.append({'file': code.co_filename[:512], 'function': code.co_name[:256],
                                  'line': frame.f_lineno})
                    frame = frame.f_back
                data.append({'thread_id': thread_id, 'frames': stack, 'truncated': frame is not None})
            payload = {'monotonic': time.monotonic(), 'threads': data, 'threads_truncated': len(all_frames) > 128}
            raw = ('GIL_STACK_JSON ' + json.dumps(payload, ensure_ascii=True) + '\nGIL_STACK_COMPLETE\n').encode('utf-8')
            while raw:
                written = os.write(self._fd, raw)
                if written <= 0:
                    raise OSError('STACK_WRITE_INCOMPLETE')
                raw = raw[written:]
            self.written = True
        except BaseException as exc:
            self.error = type(exc).__name__

    def cancel(self):
        due = time.monotonic() >= self._deadline
        self._stop.set()
        self._thread.join(1)
        return {'triggered': self.triggered, 'written': self.written,
                'error': self.error, 'thread_stopped': not self._thread.is_alive(), 'due': due}

from __future__ import annotations

import os
import time


class OwnedLinuxWitness:
    def __init__(self, pid, expected_created):
        import signal
        from pathlib import Path
        self._signal = signal
        self._handle = os.pidfd_open(pid)
        try:
            generation = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
            if generation != str(expected_created):
                raise RuntimeError('WITNESS_IDENTITY_MISMATCH')
        except BaseException:
            os.close(self._handle)
            self._handle = None
            raise

    def running(self):
        import select
        poller = select.poll()
        poller.register(self._handle, select.POLLIN)
        return not poller.poll(0)

    def terminate(self):
        if self.running():
            self._signal.pidfd_send_signal(self._handle, self._signal.SIGKILL)

    def wait(self, deadline):
        while self.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.running()

    def close(self):
        if self._handle is not None:
            os.close(self._handle)
            self._handle = None


def own_birth():
    from pathlib import Path
    return Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]


def witness(pid, birth):
    return OwnedLinuxWitness(pid, birth)

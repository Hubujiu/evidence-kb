from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


@dataclass(frozen=True)
class Outcome:
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    cleanup_complete: bool
    cleanup_seconds: float
    error: str | None

    @property
    def passed(self):
        return (self.exit_code == 0 and not self.timed_out and not self.cancelled
                and self.cleanup_complete and self.error is None)


def backend():
    if sys.platform == 'linux':
        from scripts.check_process_owner_linux import Owner
    elif sys.platform == 'win32':
        from scripts.check_process_owner_windows import Owner
    else:
        raise RuntimeError('UNSUPPORTED_PROCESS_CONTAINMENT')
    return Owner


def supervise(config, cancelled):
    owner = None
    code = None
    timed_out = False
    error = None
    cleaned = False
    cleanup_seconds = 0.0
    try:
        owner = backend()()
        deadline = time.monotonic() + config['timeout']
        if not cancelled.is_set():
            owner.spawn(config['command'], config['cwd'], config['env'], sys.stdout)
        while not cancelled.is_set():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            code = owner.poll()
            if code is not None:
                break
            cancelled.wait(min(0.01, max(0, deadline - time.monotonic())))
    except BaseException as exc:
        error = type(exc).__name__
    finally:
        started = time.monotonic()
        if owner is not None:
            try:
                cleaned = owner.cleanup(started + config['cleanup_timeout'])
            except BaseException as exc:
                error = error or type(exc).__name__
            finally:
                try:
                    owner.close()
                except BaseException as exc:
                    cleaned = False
                    error = error or type(exc).__name__
        cleanup_seconds = time.monotonic() - started
    return Outcome(code, timed_out, cancelled.is_set(), cleaned, cleanup_seconds, error)


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--supervise', action='store_true', required=True)
    args = parser.parse_args()
    assert args.supervise
    config = json.loads(sys.stdin.readline())
    cancelled = threading.Event()
    
    def watch_control():
        while os.read(sys.stdin.fileno(), 4096):
            pass
        cancelled.set()
    watcher = threading.Thread(target=watch_control, daemon=True)
    watcher.start()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: cancelled.set())
    result = supervise(config, cancelled)
    destination = Path(config['result'])
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(result.__dict__), encoding='utf-8')
    temporary.replace(destination)
    return 0 if result.passed else 2


def run_owned(command, *, cwd, env, stream, timeout, cleanup_timeout=5, cancel_event=None):
    
    if sys.platform not in ('linux', 'win32'):
        return Outcome(None, False, False, False, 0, 'UNSUPPORTED_PROCESS_CONTAINMENT')
    if timeout <= 0 or cleanup_timeout <= 0:
        raise ValueError('POSITIVE_OWNER_DEADLINES_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='evidence-gate-owner-') as temporary:
        result_path = Path(temporary) / 'result.json'
        config = dict(command=command, cwd=str(cwd), env=env, timeout=timeout,
                      cleanup_timeout=cleanup_timeout, result=str(result_path))
        process = subprocess.Popen([sys.executable, '-B', '-m',
            'scripts.check_process_owner', '--supervise'],
            cwd=Path(__file__).resolve().parents[1], env=env,
            stdin=subprocess.PIPE, stdout=stream, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', close_fds=True)
        caller_cancelled = False
        watchdog_expired = False
        try:
            process.stdin.write(json.dumps(config) + '\n')
            process.stdin.flush()
            watchdog = time.monotonic() + timeout + cleanup_timeout + 10
            while process.poll() is None:
                if time.monotonic() >= watchdog:
                    watchdog_expired = True
                    process.stdin.close()
                    break
                if cancel_event is not None and cancel_event.is_set():
                    caller_cancelled = True
                    process.stdin.close()
                    break
                time.sleep(0.01)
            if process.poll() is None:
                process.wait(timeout=cleanup_timeout + 5)
        except BaseException:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            
            try:
                process.wait(timeout=cleanup_timeout + 5)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        if not result_path.is_file():
            return Outcome(None, False, bool(cancel_event and cancel_event.is_set()),
                           False, 0, 'OWNER_RESULT_MISSING')
        result = Outcome(**json.loads(result_path.read_text('utf-8')))
        if caller_cancelled or watchdog_expired:
            result = Outcome(result.exit_code, result.timed_out,
                result.cancelled or caller_cancelled, result.cleanup_complete,
                result.cleanup_seconds, 'OWNER_WATCHDOG_EXPIRED' if watchdog_expired else result.error)
        if process.returncode != 0 and result.passed:
            return Outcome(result.exit_code, result.timed_out, result.cancelled,
                           False, result.cleanup_seconds, 'OWNER_EXIT_MISMATCH')
        return result


if __name__ == '__main__':
    raise SystemExit(_main())

import argparse
import json
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch
import sys
import subprocess
import shutil
import tempfile

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROCESS_LAUNCHER, ProcessPackages


def wait_until(predicate, seconds):
    expires = monotonic() + seconds
    while monotonic() < expires:
        if predicate():
            return True
        Event().wait(0.005)
    return bool(predicate())


def idle_exit_case(manager, installed, root):
    journal = root / 'idle-exit-journal'
    spec = manager.catalog(installed['package'], journal=journal)
    disposed = Event()

    def mount(ctx, config):
        ctx.require('external.supervision')
        ctx.defer(disposed.set)

    dependent = PluginSpec('fixture.dependent', (), ('external.supervision',), mount)
    host = PluginHost({'version': 1, 'plugins': [{'id': 'process', 'use': spec.key},
        {'id': 'dependent', 'use': dependent.key}]}, [spec, dependent])
    transport = host.service('external.supervision').transport
    marker = root / 'idle-exit-release'
    value = host.call('external.supervision', 'idle_exit', str(marker))
    leases_before_exit = host.inspect()['leases']
    marker.touch()
    completed = wait_until(lambda: transport._closed and disposed.is_set(), 3)
    snapshot = host.inspect()
    observation = {'mode': 'idle_exit', 'armed': value, 'leases_before_exit': leases_before_exit,
        'closed_without_retry': completed, 'runtime_state': snapshot['state'],
        'runtime_leases': snapshot['leases'], 'queued_actions': snapshot['counts']['queued_actions'],
        'resources': snapshot['counts']['resources'], 'dependent_disposed': disposed.is_set(),
        'writer_alive': transport._writer.is_alive(), 'reader_alive': transport._reader.is_alive(),
        'child_alive': transport._process.poll() is None}
    observation['decision'] = 'PASS' if (value == 'armed' and not leases_before_exit and completed
        and not any(observation[key] for key in ('runtime_leases', 'queued_actions', 'resources',
            'writer_alive', 'reader_alive', 'child_alive')) and disposed.is_set()) else 'FAIL'
    host.close()
    return observation


def completed_cancel_writes_case(manager, installed, root):
    journal = root / 'cancel-write-journal'
    spec = manager.catalog(installed['package'], journal=journal, timeout=15)
    host = PluginHost({'version': 1, 'plugins': [{'id': 'process', 'use': spec.key}]}, [spec])
    transport = host.service('external.supervision').transport
    count, grace = 256, 2.0
    ready, release = root / 'batch-ready', root / 'batch-release'
    cancel, completed, failures = Event(), [], []
    workers = []

    def invoke():
        try:
            completed.append(transport.request('invoke', service='external.supervision', method='batch',
                args=[count, str(ready), str(release)], kwargs={}, cancel_signal=cancel))
        except BaseException as error:
            failures.append(getattr(error, 'code', type(error).__name__))

    with patch('kb_platform.plugin_process.CANCEL_GRACE_SECONDS', grace):
        for _ in range(count):
            worker = Thread(target=invoke, daemon=True)
            workers.append(worker)
            worker.start()
        if not wait_until(ready.exists, 10):
            transport._terminate()
            raise RuntimeError('CANCEL_WRITE_BATCH_NOT_RECEIVED')
        cancel.set()
        controls_ready = wait_until(lambda: len(list(journal.glob('*-CANCEL_REQUESTED.json'))) == count, grace)
        release.touch()
        calls_completed = wait_until(lambda: len(completed) + len(failures) == count, grace)
        for worker in workers:
            worker.join(0.01)
        pending_after_completion = len(transport._pending)
        stopped = wait_until(lambda: transport._process.poll() is not None
            and not transport._writer.is_alive(), grace + 2)
    observation = {'mode': 'completed_cancel_writes', 'call_count': count, 'cancel_grace_seconds': grace,
        'controls_enqueued_before_results': controls_ready, 'all_calls_returned': calls_completed,
        'completed_calls': len(completed), 'call_errors': failures,
        'pending_after_completion': pending_after_completion, 'queued_writes': transport._outgoing.qsize(),
        'stopped_without_external_cleanup': stopped, 'writer_alive': transport._writer.is_alive(),
        'child_alive': transport._process.poll() is None}
    observation['decision'] = 'PASS' if (controls_ready and calls_completed and len(completed) == count
        and not failures and not pending_after_completion and stopped) else 'FAIL'
    transport._terminate()
    host.close()
    return observation


def blocked_pipe_cases(manager, installed, root):
    observations = []
    for mode in ('deadline', 'cancel', 'queued_deadline'):
        journal = root / ('blocked-' + mode)
        spec = manager.catalog(installed['package'], journal=journal, timeout=0.4)
        host = PluginHost({'version': 1, 'plugins': [{'id': 'blocked', 'use': spec.key}]}, [spec])
        transport = host.service('external.blocked').transport
        started, completed, failures = Event(), Event(), []
        record = transport._record

        def observe(request, status, **fields):
            record(request, status, **fields)
            if status == 'SENT' and request.get('method') == 'accept':
                started.set()

        transport._record = observe

        def invoke():
            try:
                host.bind('external.blocked').invoke('accept', args=('x' * (1024 * 1024),),
                    timeout=0.15 if mode == 'deadline' else None)
            except PluginError as error:
                failures.append(error.code)
            finally:
                completed.set()

        begin = monotonic()
        worker = Thread(target=invoke, daemon=True)
        worker.start()
        if not started.wait(3):
            transport._terminate()
            worker.join(3)
            host.close()
            raise RuntimeError('BLOCKED_PIPE_REQUEST_DID_NOT_START')
        queued_errors, queued_elapsed, first_still_running = [], None, None
        if mode == 'queued_deadline':
            queued_begin = monotonic()
            try:
                host.bind('external.blocked').invoke('accept', args=('queued',), timeout=0.05)
            except PluginError as error:
                queued_errors.append(error.code)
            queued_elapsed = monotonic() - queued_begin
            first_still_running = not completed.is_set()
        elif mode == 'cancel':
            operations = tuple(host._cancellations)
            if len(operations) != 1:
                raise RuntimeError('BLOCKED_PIPE_MISSING_RUNTIME_OPERATION')
            host.cancel(operations[0])
        bounded = completed.wait(1.5)
        elapsed = monotonic() - begin
        if not bounded:
            transport._terminate()
        worker.join(3)
        host.close()
        rows = [json.loads(path.read_text('utf-8')) for path in journal.glob('*.json')]
        unknown = [row for row in rows if row['status'] == 'UNKNOWN' and row['request'].get('method') == 'accept']
        sent = [row for row in rows if row['status'] == 'SENT' and row['request'].get('method') == 'accept']
        controls = [row['request'] for row in rows if row['status'] == 'CANCEL_REQUESTED']
        minimal_controls = all(set(row) <= {'protocol', 'id', 'kind', 'target', 'identity',
            'deadline_seconds', 'cancelled'} and len(json.dumps(row).encode()) < 1024 for row in controls)
        writer = getattr(transport, '_writer', None)
        snapshot = host.inspect()
        observation = {'mode': mode, 'completed_without_watchdog': bounded,
            'elapsed_seconds': elapsed, 'payload_bytes': 1024 * 1024, 'errors': failures,
            'sent': len(sent), 'unknown': len(unknown), 'call_thread_alive': worker.is_alive(),
            'writer_alive': writer is not None and writer.is_alive(),
            'child_alive': transport._process.poll() is None,
            'reader_alive': transport._reader.is_alive(), 'runtime_state': snapshot['state'],
            'runtime_leases': snapshot['leases'], 'minimal_cancel_messages': minimal_controls,
            'queued_errors': queued_errors, 'queued_elapsed_seconds': queued_elapsed,
            'first_running_after_queued_deadline': first_still_running}
        observation['decision'] = 'PASS' if (bounded and failures == ['PROCESS_RESULT_UNKNOWN']
            and len(sent) == len(unknown) == 1 and not any(observation[key] for key in
                ('call_thread_alive', 'writer_alive', 'child_alive', 'reader_alive'))
            and snapshot['state'] == 'disposed' and not snapshot['leases'] and minimal_controls
            and (mode != 'cancel' or len(controls) == 1)
            and (mode != 'queued_deadline' or queued_errors in (
                ['PROCESS_DEADLINE_BEFORE_SEND'], ['PROCESS_CANCELLED_BEFORE_SEND'])
                and first_still_running)) else 'FAIL'
        observations.append(observation)
    return observations


def run(output, plugin_python=None, *, blocked_only=False, supervision_only=False):
    examples = Path(__file__).resolve().parents[2] / 'examples'
    with tempfile.TemporaryDirectory(prefix='kb-process-integration-') as temporary:
        root = Path(temporary)
        manager = ProcessPackages(root / 'installed')
        packages = {}
        for name in ('process-echo', 'process-blocked-pipe', 'process-supervision'):
            source = examples / name
            if plugin_python is not None:
                copied = root / name
                shutil.copytree(source, copied)
                manifest = json.loads((copied / 'plugin.json').read_text('utf-8'))
                manifest['python'] = str(plugin_python.resolve())
                (copied / 'plugin.json').write_text(json.dumps(manifest), 'utf-8')
                source = copied
            packages[name] = manager.install(source)
        installed = packages['process-echo']
        commands = [[str(plugin_python.resolve()) if plugin_python is not None else sys.executable,
            *flags, '-I', '-u', '-c', PROCESS_LAUNCHER,
            package['package'], str(Path(package['package']) / 'plugin.py')]
            for package in packages.values() for flags in ([], ['-B'])]
        def audit(event, args):
            if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg',
                         'os.system', 'os.posix_spawn', 'os.exec'}:
                raise RuntimeError('PROCESS_FIXTURE_FORBIDS_' + event)
            if event == 'subprocess.Popen' and not any(
                    args[1] in (command, subprocess.list2cmdline(command)) for command in commands):
                raise RuntimeError('PROCESS_FIXTURE_REJECTS_OTHER_COMMAND')
        sys.addaudithook(audit)
        blocked = [] if supervision_only else blocked_pipe_cases(manager, packages['process-blocked-pipe'], root)
        supervision = [] if blocked_only else [
            idle_exit_case(manager, packages['process-supervision'], root),
            completed_cancel_writes_case(manager, packages['process-supervision'], root)]
        if supervision_only or any(row['decision'] != 'PASS' for row in supervision):
            output.mkdir(parents=True, exist_ok=False)
            (output / 'result.json').write_text(json.dumps({'decision': 'PASS' if all(
                row['decision'] == 'PASS' for row in supervision) else 'FAIL', 'supervision': supervision,
                'new_model_calls': 0, 'new_embedding_calls': 0}, indent=2), 'utf-8')
            if any(row['decision'] != 'PASS' for row in supervision):
                raise RuntimeError('PROCESS_EXIT_OR_CONTROL_WRITE_NOT_SUPERVISED')
            return
        if blocked_only or any(row['decision'] != 'PASS' for row in blocked):
            output.mkdir(parents=True, exist_ok=False)
            (output / 'result.json').write_text(json.dumps({'decision': 'PASS' if all(
                row['decision'] == 'PASS' for row in blocked) else 'FAIL', 'blocked_pipe': blocked,
                'new_model_calls': 0, 'new_embedding_calls': 0}, indent=2), 'utf-8')
            if any(row['decision'] != 'PASS' for row in blocked):
                raise RuntimeError('BLOCKED_PIPE_DEADLINE_OR_CANCELLATION_NOT_BOUNDED')
            return
        spec = manager.catalog(installed['package'], journal=root / 'journal')
        profile = {'version': 1, 'plugins': [{'id': 'echo', 'use': spec.key}]}
        host = PluginHost(profile, [spec])
        runtime_info = host.service('external.echo').transport.runtime_info
        binding = host.bind('external.echo')
        value = binding.call('echo', 'hello', named=7)
        if value['args'] != ['hello'] or value['kwargs'] != {'named': 7} or not value['operation']:
            raise RuntimeError('PROCESS_FIXTURE_VALUE_MISMATCH')
        large_payload = 'x' * (1024 * 1024)
        large = binding.call('echo', large_payload)
        if large['args'] != [large_payload] or large['kwargs'] != {}:
            raise RuntimeError('PROCESS_FIXTURE_LARGE_ROUNDTRIP_MISMATCH')
        host.drain()
        try:
            binding.call('echo', 'rejected')
        except PluginError:
            pass
        else:
            raise RuntimeError('PROCESS_FIXTURE_ADMITTED_AFTER_DRAIN')
        host.close()
        with PluginHost(profile, [spec]) as cancelled:
            waiting, completed, failures = Event(), Event(), []
            transport = cancelled.service('external.echo').transport
            original_input = transport._process.stdin

            class ObservedInput:
                def write(self, payload):
                    count = original_input.write(payload)
                    if b'"method":"wait"' in payload:
                        waiting.set()
                    return count

                def flush(self):
                    return original_input.flush()

                def close(self):
                    return original_input.close()

            transport._process.stdin = ObservedInput()
            def wait():
                try:
                    cancelled.call('external.echo', 'wait')
                except PluginError as exc:
                    failures.append(exc.code)
                finally:
                    completed.set()
            worker = Thread(target=wait)
            worker.start()
            if not waiting.wait(5):
                raise RuntimeError('PROCESS_FIXTURE_WAIT_NOT_ADMITTED')
            while not cancelled._cancellations and not completed.is_set():
                completed.wait(0.001)
            operations = tuple(cancelled._cancellations)
            if len(operations) != 1:
                raise RuntimeError('PROCESS_FIXTURE_MISSING_CALL_IDENTITY')
            cancelled.cancel(operations[0])
            worker.join(5)
            if worker.is_alive() or failures != ['PROCESS_CALL_ERROR']:
                raise RuntimeError('PROCESS_FIXTURE_CANCEL_DID_NOT_FINISH')
            try:
                cancelled.bind('external.echo').invoke('wait', timeout=0.02)
            except PluginError as exc:
                if exc.code not in {'PROCESS_CALL_ERROR', 'PROCESS_RESULT_UNKNOWN'}:
                    raise
                deadline_outcome = exc.code
            else:
                raise RuntimeError('PROCESS_FIXTURE_DEADLINE_NOT_OBSERVED')
        with PluginHost(profile, [spec]) as crashed:
            try:
                crashed.call('external.echo', 'crash')
            except PluginError as exc:
                if exc.code != 'PROCESS_RESULT_UNKNOWN':
                    raise
            else:
                raise RuntimeError('PROCESS_FIXTURE_CRASH_MISSING')
        rows = [json.loads(path.read_text('utf-8')) for path in (root / 'journal').glob('*.json')]
        unknown = [row for row in rows if row['status'] == 'UNKNOWN']
        crash_sends = [row for row in rows if row['status'] == 'SENT'
            and row['request'].get('method') == 'crash']
        if len(unknown) != 1 + int(deadline_outcome == 'PROCESS_RESULT_UNKNOWN') or len(crash_sends) != 1:
            raise RuntimeError('PROCESS_FIXTURE_UNKNOWN_OR_DUPLICATE_SEND')
        controls = [row['request'] for row in rows if row['status'] == 'CANCEL_REQUESTED']
        if not controls or any(set(row) - {'protocol', 'id', 'kind', 'target', 'identity',
                'deadline_seconds', 'cancelled'} for row in controls):
            raise RuntimeError('PROCESS_CANCEL_MESSAGE_CONTAINS_BUSINESS_ARGUMENTS')
        output.mkdir(parents=True, exist_ok=False)
        (output / 'result.json').write_text(json.dumps({'decision': 'PASS',
            'scope': 'controlled local external Python process; no provider or production quality claim',
            'installed_digest': installed['digest'], 'echo': value, 'crash_sends': len(crash_sends),
            'large_echo_bytes': len(large_payload),
            'plugin_runtime': runtime_info,
            'unknown': unknown, 'cancel_and_deadline': 'completion or process termination observed before lease release',
            'deadline_outcome': deadline_outcome,
            'blocked_pipe': blocked,
            'supervision': supervision,
            'new_model_calls': 0, 'new_embedding_calls': 0}), 'utf-8')


def main():
    parser = argparse.ArgumentParser(description='Verify controlled local process plugin transport.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plugin-python', type=Path)
    parser.add_argument('--blocked-pipe-only', action='store_true')
    parser.add_argument('--supervision-only', action='store_true')
    args = parser.parse_args()
    run(args.output, args.plugin_python, blocked_only=args.blocked_pipe_only,
        supervision_only=args.supervision_only)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Event, Thread, current_thread
from time import monotonic
from unittest.mock import patch
import sys
import subprocess
import shutil
import tempfile

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROTOCOL, PROCESS_LAUNCHER, ProcessPackages, ProcessTransport


def wait_until(predicate, seconds):
    expires = monotonic() + seconds
    while monotonic() < expires:
        if predicate():
            return True
        Event().wait(0.005)
    return bool(predicate())


def close_when_quiescent(host):
    expires = monotonic() + 3
    while True:
        try:
            host.close()
            return
        except PluginError as error:
            if error.code not in {'scope_busy', 'PLUGIN_LIFECYCLE_BUSY'} or monotonic() >= expires:
                raise
            Event().wait(0.005)


@contextmanager
def process_host(profile, catalog):
    host = PluginHost(profile, catalog)
    try:
        yield host
    finally:
        close_when_quiescent(host)


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
    close_when_quiescent(host)
    return observation


def stale_exit_case(manager, installed, root):
    spec = manager.catalog(installed['package'], journal=root / 'stale-exit-journal')
    host = PluginHost({'version': 1, 'plugins': [{'id': 'process', 'use': spec.key}]}, [spec])
    old = host.service('external.supervision').transport
    entered, release, done = Event(), Event(), Event()
    notify = ProcessTransport._notify_exit

    def delayed(transport, error):
        if transport is old:
            entered.set()
            if not release.wait(5):
                raise RuntimeError('STALE_EXIT_NOTIFICATION_BARRIER_EXPIRED')
        try:
            notify(transport, error)
        finally:
            if transport is old:
                done.set()

    try:
        with patch.object(ProcessTransport, '_notify_exit', delayed):
            marker = root / 'old-generation-exit'
            host.call('external.supervision', 'idle_exit', str(marker))
            marker.touch()
            if not entered.wait(3):
                raise RuntimeError('STALE_EXIT_NOTIFICATION_NOT_SCHEDULED')
            old._process.wait(timeout=3)
            host.update('process', config={'timeout': 3})
            current = host.service('external.supervision').transport
            before = host.call('external.supervision', 'idle_exit', str(root / 'new-generation-exit'))
            release.set()
            if not done.wait(3):
                raise RuntimeError('STALE_EXIT_NOTIFICATION_DID_NOT_FINISH')
            after = host.call('external.supervision', 'idle_exit', str(root / 'new-generation-exit'))
            snapshot = host.inspect()
            observation = {'mode': 'stale_exit', 'old_pid': old._process.pid,
                'current_pid': current._process.pid, 'old_generation': old.identity['generation'],
                'current_generation': current.identity['generation'], 'call_before_notice': before,
                'call_after_notice': after, 'current_alive': current._process.poll() is None,
                'runtime_state': snapshot['state'], 'desired': snapshot['instances'][0]['desired']}
            observation['decision'] = 'PASS' if (before == after == 'armed'
                and observation['current_alive'] and observation['desired'] == 'enabled'
                and observation['runtime_state'] == 'ready'
                and observation['current_generation'] != observation['old_generation']) else 'FAIL'
            return observation
    finally:
        release.set()
        close_when_quiescent(host)


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
    close_when_quiescent(host)
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
            close_when_quiescent(host)
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
        close_when_quiescent(host)
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


def generic_command_case(root):
    source = root / 'command-source'
    source.mkdir()
    (source / 'worker.py').write_text(
        'import json, os, sys\n'
        'for line in sys.stdin:\n'
        '    request = json.loads(line)\n'
        '    result = {"argv": sys.argv[1:], "cwd": os.getcwd(), "input": request.get("args")}\n'
        '    if request["kind"] == "handshake":\n'
        '        result = {"protocol": request["protocol"], "services": request["capabilities"],\n'
        '            "runtime": {"language": "python", "version": sys.version, "executable": sys.executable}}\n'
        '    print(json.dumps({"protocol": request["protocol"], "id": request["id"],\n'
        '        "ok": True, "result": result}), flush=True)\n'
        '    if request["kind"] == "shutdown":\n'
        '        break\n', 'utf-8')
    arguments = ['', 'two words', 'literal&value', '$unchanged']
    (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
        'version': '1', 'command': [sys.executable, '-B', '-u', 'worker.py', *arguments],
        'services': {'echo': ['invoke']}}), 'utf-8')
    manager = ProcessPackages(Path(root) / 'installed')
    installed = manager.install(source)
    transport = ProcessTransport(installed['package'], journal=Path(root) / 'journal',
        timeout=3, identity={})
    try:
        transport.start()
        result = transport.request('invoke', service='echo', method='invoke', args=['hello'], kwargs={})
    finally:
        transport.close()
    rows = [json.loads(path.read_text('utf-8')) for path in (Path(root) / 'journal').glob('*.json')]
    accepted = (result == {'argv': arguments, 'cwd': installed['package'], 'input': ['hello']}
        and transport.runtime_info['language'] == 'python' and transport._process.poll() is not None
        and not transport._reader.is_alive() and not transport._writer.is_alive()
        and sum(row['status'] == 'COMPLETE' for row in rows) == 3
        and not any(row['status'] == 'UNKNOWN' for row in rows))
    return {'decision': 'PASS' if accepted else 'FAIL', 'runtime': transport.runtime_info,
        'result': result, 'child_stopped': transport._process.poll() is not None}


def dependency_calls_case(manager, installed, root):
    observations, calls = [], []
    journal = root / 'dependency-journal'
    spec = manager.catalog(installed['package'], journal=journal)
    caller_thread = current_thread()

    class SharedModel:
        def generate(self, prompt):
            calls.append({'prompt': prompt, 'same_thread': current_thread() is caller_thread,
                'operation': host.current_call['operation'], 'deadline': host.current_call['deadline']})
            return {'answer': prompt, 'model': 'deterministic-fixture'}

    def mount(ctx, config):
        ctx.provide('shared.model', SharedModel())

    model = PluginSpec('fixture.shared-model', ('shared.model',), (), mount)
    profile = {'version': 1, 'plugins': [
        {'id': 'model', 'use': model.key}, {'id': 'consumer', 'use': spec.key}]}
    with process_host(profile, [model, spec]) as host:
        transport = host.service('external.consumer').transport
        dependency = transport.invoke_dependency

        def observe(call, timeout):
            observations.append({'parent_operation': host.current_call['operation'],
                'parent_deadline': host.current_call['deadline'], 'same_thread': current_thread() is caller_thread})
            return dependency(call, timeout)

        transport.invoke_dependency = observe
        result = host.bind('external.consumer').invoke('invoke', args=('hello',), timeout=3)
        denied = host.call('external.consumer', 'denied', 'denied')
        runtime = transport.runtime_info
        leases = host.inspect()['leases']
    rows = [json.loads(path.read_text('utf-8')) for path in journal.glob('*.json')]
    parent = next(row['request']['identity'] for row in rows if row['status'] == 'PREPARED'
        and row['request'].get('method') == 'invoke')
    accepted = (result['ok'] and result['result'] == {'answer': 'hello:continued', 'model': 'deterministic-fixture'}
        and not denied['ok'] and denied['error']['code'] == 'UNDECLARED_SERVICE_DEPENDENCY'
        and len(calls) == len(observations) == 2 and not leases
        and all(row['same_thread'] and row['parent_operation'] == parent['operation']
            and row['parent_deadline'] == parent['deadline'] for row in observations)
        and all(row['same_thread'] and row['operation'] != parent['operation']
            and row['deadline'] <= parent['deadline'] for row in calls))
    malformed = []
    for method in ('unknown_parent', 'duplicate'):
        before = len(calls)
        with process_host(profile, [model, spec]) as host:
            try:
                host.call('external.consumer', method, method)
            except PluginError as error:
                failure = error.code
            else:
                failure = 'UNEXPECTED_SUCCESS'
        count = len(calls) - before
        malformed.append({'method': method, 'error': failure, 'dependency_calls': count})
        accepted = accepted and failure == 'PROCESS_RESULT_UNKNOWN' and count <= (method == 'duplicate')
    return {'decision': 'PASS' if accepted else 'FAIL', 'runtime': runtime,
        'result': result, 'undeclared_dependency': denied, 'callbacks': observations, 'provider_calls': calls[:2],
        'remaining_leases': leases, 'malformed_callbacks': malformed}


def run(output, plugin_python=None, *, plugin_node=None, blocked_only=False, supervision_only=False):
    examples = Path(__file__).resolve().parents[2] / 'examples'
    with tempfile.TemporaryDirectory(prefix='kb-process-integration-') as temporary:
        root = Path(temporary)
        command_case = generic_command_case(root)
        if command_case['decision'] != 'PASS':
            raise RuntimeError('PROCESS_COMMAND_LAUNCH_CONTRACT_FAILED')
        manager = ProcessPackages(root / 'installed')
        packages = {}
        for name in ('process-echo', 'process-blocked-pipe', 'process-supervision', 'process-dependency'):
            source = examples / name
            if plugin_python is not None or plugin_node is not None and name in {'process-echo', 'process-dependency'}:
                copied = root / name
                shutil.copytree(source, copied)
                manifest = json.loads((copied / 'plugin.json').read_text('utf-8'))
                if plugin_node is not None and name in {'process-echo', 'process-dependency'}:
                    manifest.pop('entry')
                    manifest['command'] = [str(plugin_node.resolve()), 'plugin.mjs']
                else:
                    manifest['python'] = str(plugin_python.resolve())
                (copied / 'plugin.json').write_text(json.dumps(manifest), 'utf-8')
                source = copied
            packages[name] = manager.install(source)
        installed = packages['process-echo']
        commands = [package['manifest']['command'] if 'command' in package['manifest'] else
            [str(plugin_python.resolve()) if plugin_python is not None else sys.executable,
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
        dependencies = None if blocked_only or supervision_only else dependency_calls_case(
            manager, packages['process-dependency'], root)
        if dependencies is not None and dependencies['decision'] != 'PASS':
            raise RuntimeError('PROCESS_DEPENDENCY_CALL_CONTRACT_FAILED')
        blocked = [] if supervision_only else blocked_pipe_cases(manager, packages['process-blocked-pipe'], root)
        supervision = [] if blocked_only else [
            idle_exit_case(manager, packages['process-supervision'], root),
            stale_exit_case(manager, packages['process-supervision'], root),
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
        close_when_quiescent(host)
        with process_host(profile, [spec]) as cancelled:
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
            prior_requests = {path.name for path in (root / 'journal').glob('*-PREPARED.json')}
            try:
                cancelled.bind('external.echo').invoke('wait', timeout=0.02)
            except PluginError as exc:
                if exc.code not in {'PROCESS_CALL_ERROR', 'PROCESS_RESULT_UNKNOWN',
                        'PROCESS_CANCELLED_BEFORE_SEND', 'PROCESS_DEADLINE_BEFORE_SEND'}:
                    raise
                deadline_outcome = exc.code
            else:
                raise RuntimeError('PROCESS_FIXTURE_DEADLINE_NOT_OBSERVED')
            deadline_requests = {json.loads(path.read_text('utf-8'))['request']['id']
                for path in (root / 'journal').glob('*-PREPARED.json') if path.name not in prior_requests}
            deadline_sends = [path for path in (root / 'journal').glob('*-SENT.json')
                if json.loads(path.read_text('utf-8'))['request']['id'] in deadline_requests]
            if deadline_outcome.endswith('_BEFORE_SEND') and deadline_sends:
                raise RuntimeError('PROCESS_FIXTURE_PRE_SEND_DEADLINE_WAS_SENT')
            if not deadline_outcome.endswith('_BEFORE_SEND') and len(deadline_sends) != 1:
                raise RuntimeError('PROCESS_FIXTURE_POST_SEND_DEADLINE_MISSING_SEND')
        pre_send_journal = root / 'pre-send-journal'
        pre_send_spec = manager.catalog(installed['package'], journal=pre_send_journal)
        with process_host(profile, [pre_send_spec]) as pre_send:
            transport = pre_send.service('external.echo').transport
            record = transport._record

            def wait_for_deadline(request, status, **fields):
                record(request, status, **fields)
                if status == 'PREPARED' and request.get('method') == 'wait':
                    signal = pre_send._cancellations[request['identity']['operation']]
                    if not signal.wait(2):
                        raise RuntimeError('PROCESS_FIXTURE_PRE_SEND_DEADLINE_NOT_SIGNALLED')

            transport._record = wait_for_deadline
            try:
                pre_send.bind('external.echo').invoke('wait', timeout=0.02)
            except PluginError as exc:
                if exc.code not in {'PROCESS_CANCELLED_BEFORE_SEND', 'PROCESS_DEADLINE_BEFORE_SEND'}:
                    raise
                pre_send_outcome = exc.code
            else:
                raise RuntimeError('PROCESS_FIXTURE_PRE_SEND_DEADLINE_NOT_REJECTED')
            pre_send_rows = [json.loads(path.read_text('utf-8'))
                for path in pre_send_journal.glob('*.json')]
            pre_send_sends = [row for row in pre_send_rows if row['status'] == 'SENT'
                and row['request'].get('method') == 'wait']
            if pre_send_sends or pre_send.inspect()['leases']:
                raise RuntimeError('PROCESS_FIXTURE_PRE_SEND_DEADLINE_LEAKED_SEND_OR_LEASE')
            if pre_send.call('external.echo', 'echo', 'healthy')['args'] != ['healthy']:
                raise RuntimeError('PROCESS_FIXTURE_PRE_SEND_DEADLINE_BROKE_TRANSPORT')
        with process_host(profile, [spec]) as crashed:
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
            'scope': 'controlled local external process; no provider or production quality claim',
            'installed_digest': installed['digest'], 'echo': value, 'crash_sends': len(crash_sends),
            'large_echo_bytes': len(large_payload),
            'plugin_runtime': runtime_info,
            'dependency_calls': dependencies,
            'command_launch': command_case,
            'unknown': unknown, 'cancel_and_deadline': 'completion or process termination observed before lease release',
            'deadline_outcome': deadline_outcome,
            'deadline_sends': len(deadline_sends), 'pre_send_deadline_outcome': pre_send_outcome,
            'pre_send_deadline_sends': len(pre_send_sends),
            'blocked_pipe': blocked,
            'supervision': supervision,
            'new_model_calls': 0, 'new_embedding_calls': 0}), 'utf-8')


def main():
    parser = argparse.ArgumentParser(description='Verify controlled local process plugin transport.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plugin-python', type=Path)
    parser.add_argument('--plugin-node', type=Path)
    parser.add_argument('--blocked-pipe-only', action='store_true')
    parser.add_argument('--supervision-only', action='store_true')
    args = parser.parse_args()
    run(args.output, args.plugin_python, plugin_node=args.plugin_node, blocked_only=args.blocked_pipe_only,
        supervision_only=args.supervision_only)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

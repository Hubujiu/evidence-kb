import argparse
import json
from pathlib import Path
from threading import Event, Thread
import sys
import subprocess
import shutil
import tempfile

from kb_platform.plugin_host import PluginHost
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROCESS_LAUNCHER, ProcessPackages


def run(output, plugin_python=None):
    source = Path(__file__).resolve().parents[2] / 'examples/process-echo'
    with tempfile.TemporaryDirectory(prefix='kb-process-integration-') as temporary:
        root = Path(temporary)
        if plugin_python is not None:
            copied = root / 'source'
            shutil.copytree(source, copied)
            manifest = json.loads((copied / 'plugin.json').read_text('utf-8'))
            manifest['python'] = str(plugin_python.resolve())
            (copied / 'plugin.json').write_text(json.dumps(manifest), 'utf-8')
            source = copied
        manager = ProcessPackages(root / 'installed')
        installed = manager.install(source)
        entry = str(Path(installed['package']) / 'plugin.py')
        def audit(event, args):
            if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg',
                         'os.system', 'os.posix_spawn', 'os.exec'}:
                raise RuntimeError('PROCESS_FIXTURE_FORBIDS_' + event)
            command = [str(plugin_python.resolve()) if plugin_python is not None else sys.executable,
                '-I', '-u', '-c', PROCESS_LAUNCHER,
                installed['package'], entry]
            if event == 'subprocess.Popen' and args[1] not in (command, subprocess.list2cmdline(command)):
                raise RuntimeError('PROCESS_FIXTURE_REJECTS_OTHER_COMMAND')
        sys.addaudithook(audit)
        spec = manager.catalog(installed['package'], journal=root / 'journal')
        profile = {'version': 1, 'plugins': [{'id': 'echo', 'use': spec.key}]}
        host = PluginHost(profile, [spec])
        runtime_info = host.service('external.echo').transport.runtime_info
        binding = host.bind('external.echo')
        value = binding.call('echo', 'hello', named=7)
        if value['args'] != ['hello'] or value['kwargs'] != {'named': 7} or not value['operation']:
            raise RuntimeError('PROCESS_FIXTURE_VALUE_MISMATCH')
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
            original = cancelled._invoke
            def notify(binding, method, args, kwargs, **options):
                waiting.set()
                return original(binding, method, args, kwargs, **options)
            cancelled._invoke = notify
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
                if exc.code != 'PROCESS_CALL_ERROR':
                    raise
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
        if len(unknown) != 1 or len(crash_sends) != 1:
            raise RuntimeError('PROCESS_FIXTURE_UNKNOWN_OR_DUPLICATE_SEND')
        output.mkdir(parents=True, exist_ok=False)
        (output / 'result.json').write_text(json.dumps({'decision': 'PASS',
            'scope': 'controlled local external Python process; no provider or production quality claim',
            'installed_digest': installed['digest'], 'echo': value, 'crash_sends': len(crash_sends),
            'plugin_runtime': runtime_info,
            'unknown': unknown, 'cancel_and_deadline': 'cooperative termination observed before lease release',
            'new_model_calls': 0, 'new_embedding_calls': 0}), 'utf-8')


def main():
    parser = argparse.ArgumentParser(description='Verify controlled local process plugin transport.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plugin-python', type=Path)
    args = parser.parse_args()
    run(args.output, args.plugin_python)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

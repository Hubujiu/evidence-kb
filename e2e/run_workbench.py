import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    from kb_platform.storage_catalog import initialize_configuration
    from kb_platform.rag_testing.browser_integration import checked_native
    from e2e.run_web_chat import arguments, run_browser

    args = arguments('Run offline workbench browser acceptance')
    host, library = checked_native(args.checker_summary)

    node = shutil.which('node')
    runner = ROOT / 'e2e/node_modules/e2e/dist/cli/bin.js'
    if not node or not runner.is_file() or not (ROOT / 'web/dist/index.html').is_file():
        raise RuntimeError('Build web/ and run npm ci in e2e/ first')
    args.output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix='kb-workbench-e2e-') as directory:
        root = Path(directory)
        storage = json.loads((ROOT / 'config/storage.local.example.json').read_text('utf-8'))
        for name, instance in storage['instances'].items():
            instance['config']['root'] = str(root / name)
        config = root / 'storage.json'
        config.write_text(json.dumps(initialize_configuration(storage)), encoding='utf-8')
        identities = root / 'identities.json'
        identities.write_text(json.dumps({
            'synthetic-web-token-for-offline-browser-acceptance': {'tenant_id': 'test', 'principal_id': 'owner'},
            'synthetic-other-token-for-offline-browser-acceptance': {'tenant_id': 'other', 'principal_id': 'owner'},
        }), encoding='utf-8')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        env = {**{key: value for key, value in os.environ.items() if not key.startswith('KB_')}, 'KB_STORAGE_ROOT': str(root / 'store'), 'KB_STORAGE_CONFIG': str(config),
               'KB_IDENTITIES_FILE': str(identities), 'KB_REMOTE_MODELS': '0',
               'KB_DEFER_DEFAULT_APP': '1', 'KB_E2E_URL': url,
               'KB_E2E_TOKEN': 'synthetic-web-token-for-offline-browser-acceptance',
               'KB_E2E_OTHER_TOKEN': 'synthetic-other-token-for-offline-browser-acceptance',
               'KB_AGENT_KERNEL_LIBRARY': str(library),
               'KB_E2E_TESTS': 'workbench.e2e.js',
               'E2E_TELEMETRY_DISABLED': '1'}
        stop = root / 'host.stop'
        profile = root / 'profile.json'
        profile.write_text(json.dumps({'version': 1, 'worker': [sys.executable, '-B', '-u', '-m', 'kb_platform.host_worker'],
            'cwd': str(ROOT), 'stop_file': str(stop), 'application': {'factory': 'kb_platform.host_applications:api',
            'config': {'host': '127.0.0.1', 'port': port, 'log_level': 'warning'}}}), encoding='utf-8')
        process = subprocess.Popen([str(host), '--profile', str(profile)], cwd=ROOT, env=env)
        try:
            deadline = time.monotonic() + 45
            with httpx.Client(trust_env=False, timeout=1) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('API exited before readiness')
                    try:
                        if client.get(url + '/health').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError('API readiness deadline exceeded')
                    time.sleep(0.1)
            return run_browser(node, runner, args.output, env)
        finally:
            stop.touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    sys.exit(main())

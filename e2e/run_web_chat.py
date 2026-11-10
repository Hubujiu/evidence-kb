import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_for(url, process):
    deadline = time.monotonic() + 45
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'Server exited before readiness: {url}')
            try:
                if client.get(url).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise TimeoutError(f'Server did not become ready: {url}')


def browser_verdict(output, expected_file):
    run = json.loads((output / 'report.json').read_text('utf-8'))['run']
    results = run['results']
    if (run['status'] != 'passed' or run['exitCode'] != 0 or run['errors']
            or len(results) != 1 or results[0]['file'] != expected_file
            or results[0]['status'] != 'passed' or len(results[0]['attempts']) != 1
            or results[0]['attempts'][0]['status'] != 'passed'
            or run['usage']['modelTokens'] != 0 or run['usage']['maxModelCallsInStep'] != 0):
        raise ValueError('Expected one passing browser case without skips, retries or model calls')
    assertions = [step for step in results[0]['attempts'][0]['steps'] if step['kind'] == 'assertion']
    if not assertions or any(step['status'] != 'passed' for step in assertions):
        raise ValueError('Browser acceptance requires passing deterministic assertions')
    return {'tests': 1, 'assertions': len(assertions), 'failures': 0, 'errors': 0,
            'skipped': 0, 'retries': 0, 'new_model_calls': 0,
            'new_embedding_calls': 0, 'new_judge_calls': 0}


def arguments(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checker-summary', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.checker_summary = args.checker_summary.resolve()
    if args.output.exists():
        parser.error('--output must be a new directory')
    return args


def run_browser(node, runner, output, env):
    browser_output = ROOT / 'e2e/.artifacts' / uuid4().hex
    report = {'status': 'FAIL', 'scope': 'offline synthetic browser component acceptance',
              'browser_output': 'browser'}
    try:
        result = subprocess.run([node, str(runner), 'run', '--output', str(browser_output),
                                 '--no-cache', '--retries', '0'],
                                cwd=ROOT / 'e2e', env=env, check=False, timeout=180)
        if browser_output.is_dir():
            shutil.copytree(browser_output, output / 'browser')
        if result.returncode:
            raise ValueError(f'Browser runner exited with {result.returncode}')
        report.update(browser_verdict(output / 'browser', env['KB_E2E_TESTS']))
        report['status'] = 'PASS'
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        report['error'] = str(exc)
    (output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'PASS' else 1


def main():
    from kb_platform.rag_testing.browser_integration import checked_native

    args = arguments('Run offline web conversation browser acceptance')
    _, library = checked_native(args.checker_summary)
    node = shutil.which('node')
    vite = ROOT / 'web/node_modules/vite/bin/vite.js'
    e2e = ROOT / 'e2e/node_modules/e2e/dist/cli/bin.js'
    if not node or not vite.is_file() or not e2e.is_file():
        raise RuntimeError('Run npm ci in web/ and e2e/ first')
    args.output.mkdir(parents=True, exist_ok=False)
    api_port, web_port = port(), port()
    api_url, web_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{web_port}'
    space = TemporaryDirectory(prefix='kb-web-e2e-')
    env = {**{key: value for key, value in os.environ.items() if not key.startswith('KB_')},
           'KB_AGENT_KERNEL_LIBRARY': str(library), 'KB_REMOTE_MODELS': '0',
           'KB_WEB_FIXTURE_DIR': space.name, 'KB_API_URL': api_url, 'KB_E2E_URL': web_url,
           'KB_E2E_TESTS': 'web_chat.e2e.js', 'E2E_TELEMETRY_DISABLED': '1'}
    processes = []
    try:
        processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'e2e.web_fixture:app',
            '--host', '127.0.0.1', '--port', str(api_port)], cwd=ROOT, env=env))
        wait_for(api_url + '/scopes', processes[-1])
        processes.append(subprocess.Popen([node, str(vite), '--host', '127.0.0.1', '--port', str(web_port),
            '--strictPort'], cwd=ROOT / 'web', env=env))
        wait_for(web_url, processes[-1])
        return run_browser(node, e2e, args.output, env)
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        space.cleanup()


if __name__ == '__main__':
    sys.exit(main())

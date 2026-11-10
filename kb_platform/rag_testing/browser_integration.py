import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

import httpx

from .runner import write_json
from .runtime_integration import acceptance_migration_dsn, controlled_storage
from .runtime_integration import acceptance_dsn as acceptance_dsn


ROOT = Path(__file__).resolve().parents[2]
BROWSER_ASSETS = (('/', 'web/dist/index.html', 'text/html'),
                  ('/console', 'kb_platform/console.html', 'text/html'),
                  ('/console.mjs', 'kb_platform/console.mjs', 'text/javascript'),
                  ('/agent', 'web/dist/index.html', 'text/html'),
                  ('/agent/console', 'kb_platform/agent_console.html', 'text/html'),
                  ('/agent_console.mjs', 'kb_platform/agent_console.mjs', 'text/javascript'))
CASES = frozenset({
    'source upload, BM25 publication, citation and original download [chromium]',
    'switching tenants clears and isolates knowledge bases [chromium]',
})


def checked_native(summary_path):
    summary = json.loads(summary_path.read_text('utf-8'))
    if summary.get('decision') != 'PASS':
        raise ValueError('Browser acceptance requires a passing project check')
    selected = []
    for field in ('host_binary', 'native_library'):
        path = Path(summary.get(field) or '').resolve()
        if not path.is_file() or path.parent != summary_path.resolve().parent / 'native':
            raise ValueError('Browser acceptance requires the checker native snapshot')
        selected.append(path)
    return tuple(selected)


def browser_verdict(path, *, expected=CASES, classname='knowledge.e2e.js'):
    document = ET.parse(path).getroot()
    cases = list(document.iter('testcase'))
    if (len(cases) != len(expected) or {case.get('name') for case in cases} != expected
            or any(case.get('classname') != classname for case in cases)
            or any(case.find(tag) is not None for case in cases for tag in ('failure', 'error', 'skipped'))
            or any((case.findtext('system-out') or '').startswith('flaky:') for case in cases)):
        raise ValueError('Every expected browser case must pass without skips or retries')
    return {'tests': len(cases), 'failures': 0, 'errors': 0, 'skipped': 0}


def checked_browser_assets(client, url):
    observed = {}
    for path, name, media_type in BROWSER_ASSETS:
        response = client.get(url + path)
        expected = (ROOT / name).read_bytes()
        if (response.status_code != 200 or response.content != expected
                or response.headers.get('content-type', '').split(';')[0] != media_type
                or response.headers.get('cache-control') != 'no-store'
                or response.headers.get('x-content-type-options') != 'nosniff'):
            raise ValueError(f'Browser asset {path} must expose the exact current source and expected response headers')
        observed[path] = {'status': response.status_code, 'sha256': sha256(response.content).hexdigest(),
                          'bytes': len(response.content), 'content_type': response.headers['content-type']}
    return observed


def run(summary, output, dsn, migration_dsn=None):
    migration_dsn = migration_dsn or os.environ['KB_INTEGRATION_MIGRATION_DSN']
    database = acceptance_migration_dsn(dsn, migration_dsn)
    host, library = checked_native(summary)
    node = shutil.which('node')
    cli = ROOT / 'e2e/node_modules/e2e/dist/cli/bin.js'
    if node is None or not cli.is_file():
        raise ValueError('Install the locked e2e dependencies and Node first')
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    browser_output = ROOT / 'e2e/.artifacts' / run_id
    token, other_token = 'owner-' + uuid4().hex, 'other-' + uuid4().hex
    identities = output / 'identities.json'
    write_json(identities, {
        token: {'tenant_id': run_id, 'principal_id': 'owner'},
        other_token: {'tenant_id': run_id + '-other', 'principal_id': 'owner'},
    })
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    stop = output / 'host.stop'
    profile = output / 'profile.json'
    write_json(profile, {'version': 1, 'worker': [sys.executable, '-B', '-u', '-m', 'kb_platform.host_worker'],
        'cwd': str(ROOT), 'stop_file': str(stop),
        'application': {'factory': 'kb_platform.host_applications:api',
                        'config': {'host': '127.0.0.1', 'port': port, 'log_level': 'warning'}}})
    storage_observations = {}
    storage_context = controlled_storage(dsn, migration_dsn, output, storage_observations)
    storage_file, credentials = storage_context.__enter__()
    env = {**{key: value for key, value in os.environ.items() if not key.startswith('KB_')},
        **credentials, 'KB_STORAGE_CONFIG': str(storage_file), 'PGHOSTADDR': '127.0.0.1',
        'KB_STORAGE_ROOT': str(output / 'originals'), 'KB_IDENTITIES_FILE': str(identities),
        'KB_REMOTE_MODELS': '0', 'KB_ENABLE_EMBEDDINGS': '0', 'KB_DEFER_DEFAULT_APP': '1',
        'KB_AGENT_KERNEL_LIBRARY': str(library), 'PYTHONUTF8': '1',
        'E2E_TELEMETRY_DISABLED': '1', 'KB_E2E_TESTS': 'knowledge.e2e.js', 'KB_E2E_URL': url,
        'KB_E2E_TOKEN': token, 'KB_E2E_OTHER_TOKEN': other_token}
    report = {'status': 'FAIL', 'database': database, 'new_model_calls': None,
        'new_embedding_calls': 0, 'new_judge_calls': 0,
        'scope': 'synthetic browser component acceptance through Rust host, HTTP, parser and PostgreSQL',
        'checker_summary_sha256': sha256(summary.read_bytes()).hexdigest(),
        'native_sha256': {path.name: sha256(path.read_bytes()).hexdigest() for path in (host, library)}}
    process = None
    try:
        with (output / 'host.stdout.log').open('w', encoding='utf-8') as stdout, (
                output / 'host.stderr.log').open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen([str(host), '--profile', str(profile)], cwd=ROOT, env=env,
                                       stdout=stdout, stderr=stderr)
            try:
                deadline = time.monotonic() + 30
                with httpx.Client(trust_env=False, timeout=1) as client:
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('Rust host exited before HTTP readiness')
                        try:
                            if client.get(url + '/health').status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Rust host HTTP readiness deadline exceeded')
                        time.sleep(0.1)
                    report['browser_assets'] = checked_browser_assets(client, url)
                with (output / 'browser.log').open('w', encoding='utf-8') as log:
                    result = subprocess.run([node, str(cli), 'run', '--output', str(browser_output),
                        '--no-cache', '--retries', '0'], cwd=ROOT / 'e2e', env=env,
                        stdout=log, stderr=subprocess.STDOUT, timeout=180, check=False)
                report['browser_exit_code'] = result.returncode
                if result.returncode:
                    raise RuntimeError('Browser acceptance failed; see browser.log')
                report.update(browser_verdict(browser_output / 'junit.xml'))
                browser_run = json.loads((browser_output / 'report.json').read_text('utf-8'))['run']
                report['browser_usage'] = browser_run['usage']
                if (browser_run['status'] != 'passed' or browser_run['errors']
                        or browser_run['usage']['modelTokens'] != 0
                        or browser_run['usage']['maxModelCallsInStep'] != 0):
                    raise RuntimeError('Browser acceptance requires deterministic steps without model calls')
                from .console_browser_integration import run_additional_cases, run_cases

                report['console_state'] = run_cases(output / 'console-state', env)
                report['additional_state'] = run_additional_cases(output / 'additional-state', env)
            finally:
                stop.touch()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise RuntimeError('Rust host did not stop gracefully') from None
        report['host_exit_code'] = process.returncode
        report['shutdown'] = json.loads((output / 'host.stdout.log').read_text('utf-8'))
        if process.returncode or report['shutdown'].get('status') != 'completed':
            raise RuntimeError('Rust host did not report complete lifecycle cleanup')
        report['status'] = 'PASS'
    except Exception as error:
        report['error'] = str(error)
    finally:
        additional = output / 'additional-state/report.json'
        if additional.is_file():
            try:
                report['additional_state'] = json.loads(additional.read_text('utf-8'))
            except (OSError, ValueError) as error:
                report['usage_error'] = str(error)
        knowledge_usage = report.get('browser_usage') or {}
        report['zero_model_usage_verified'] = (
            all(type(knowledge_usage.get(key)) is int and knowledge_usage[key] == 0
                for key in ('modelTokens', 'maxModelCallsInStep'))
            and report.get('console_state', {}).get('zero_model_usage_verified') is True
            and isinstance(report.get('additional_state'), dict)
            and report['additional_state'].get('zero_model_usage_verified') is True)
        report['new_model_calls'] = 0 if report['zero_model_usage_verified'] else None
        if browser_output.exists():
            shutil.copytree(browser_output, output / 'browser')
        try:
            storage_context.__exit__(None, None, None)
        except Exception as error:
            report['status'], report['cleanup_error'] = 'FAIL', str(error)
        report['storage'] = storage_observations
        write_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Run deterministic tester-army/e2e browser acceptance with no model calls.')
    parser.add_argument('--checker-summary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run(args.checker_summary.resolve(), args.output.resolve(), os.environ['KB_INTEGRATION_DSN'])
    print(json.dumps({key: report[key] for key in ('status', 'database')}))
    raise SystemExit(0 if report['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()

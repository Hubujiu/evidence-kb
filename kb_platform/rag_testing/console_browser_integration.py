import argparse
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import time
from uuid import uuid4

from .browser_integration import browser_verdict
from .runner import write_json


ROOT = Path(__file__).resolve().parents[2]
CASES = frozenset(name + ' [chromium]' for name in (
    'newer citation survives older metadata completion',
    'late preview body cannot mix citations or revoke the current URL',
    'stale citation fetch failure does not replace the newer view status',
    'scope switch rejects late error-body status and successful preview body',
    'current errors remain visible and scope reset releases its preview URL once',
    'current search failure remains visible after its intentional view reset',
    'queued citation failure rechecks ownership at the final status write',
))
DRAFT_CASES = frozenset(name + ' [chromium]' for name in (
    'new draft typed during a pending turn survives its successful completion',
    'identical text retyped after submission is a new draft, not the submitted owner',
    'new draft typed during the session refresh survives the older turn',
    'new draft typed during implicit session creation is not owned by the first turn',
    'unchanged submitted draft clears while unavailable budget stays explicitly unknown',
    'unchanged draft submitted through implicit session creation still clears',
))
SCOPE_CASES = frozenset(name + ' [chromium]' for name in (
    'concurrent create cannot publish an old upload result under the newly selected scope',
    'current upload still publishes its actual DOM result in the owning scope',
    'concurrent create retains its later clear after an older search completion',
    'concurrent create cancels the next publish mutation from an older build',
    'concurrent create rejects the final status of an already sent publish',
    'current publish sends once and renders the acknowledged manifest',
))


def observed_usage(path, expected, returncode):
    unknown = {'usage_observed': False, 'usage_observation': 'UNKNOWN', 'browser_usage': None,
               'zero_model_usage_verified': False, 'new_model_calls': None}
    try:
        run = json.loads(path.read_text('utf-8'))['run']
        usage = run['usage']
        if (not isinstance(usage, dict) or returncode not in {0, 1}
                or type(run.get('exitCode')) is not int or run['exitCode'] != returncode
                or run.get('status') != ('passed' if returncode == 0 else 'failed')
                or run.get('errors') != []
                or any(type(usage.get(key)) is not int or usage[key] < 0 for key in
                       ('discoveredResults', 'modelTokens', 'maxModelCallsInStep'))
                or usage['discoveredResults'] != len(expected)):
            return unknown
        zero = usage['modelTokens'] == 0 and usage['maxModelCallsInStep'] == 0
        return {'usage_observed': True, 'usage_observation': 'OBSERVED', 'browser_usage': usage,
                'zero_model_usage_verified': zero, 'new_model_calls': 0 if zero else None}
    except (OSError, ValueError, KeyError, TypeError):
        return unknown


def run_cases(output, env, *, tests='console-state.e2e.js', expected=CASES,
              sources=('console.html', 'console.mjs')):
    output.mkdir(parents=True, exist_ok=False)
    node = shutil.which('node')
    if node is None:
        raise ValueError('Node is required for controlled console browser checks')
    browser_output = ROOT / 'e2e/.artifacts' / uuid4().hex
    command = [node, str(ROOT / 'e2e/node_modules/e2e/dist/cli/bin.js'), 'run',
               '--output', str(browser_output), '--no-cache', '--retries', '0']
    report = {'status': 'RUNNING', 'usage_observed': False, 'usage_observation': 'UNKNOWN',
              'browser_usage': None, 'new_model_calls': None, 'zero_model_usage_verified': False,
              'policy': {'modelTokens': 0, 'maxModelCallsInStep': 0, 'retries': 0}}
    try:
        with (output / 'browser.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(command, cwd=ROOT / 'e2e', env={**env,
                'KB_E2E_TESTS': tests, 'E2E_TELEMETRY_DISABLED': '1'},
                stdout=log, stderr=subprocess.STDOUT, timeout=180, check=False)
        report.update(browser_exit_code=result.returncode,
                      **observed_usage(browser_output / 'report.json', expected, result.returncode))
        if result.returncode:
            raise RuntimeError(f'Console browser regression failed with exit {result.returncode}; see {output / "browser.log"}')
        if not report['zero_model_usage_verified']:
            raise RuntimeError('Console browser checks require observed complete zero-model usage')
        report.update(browser_verdict(browser_output / 'junit.xml', expected=expected, classname=tests))
        report.update(status='PASS', source_sha256={
            name: sha256((ROOT / 'kb_platform' / name).read_bytes()).hexdigest()
            for name in sources})
        return report
    except Exception as error:
        report.update(status='FAIL', error=str(error))
        raise
    finally:
        if report['status'] == 'RUNNING':
            report['status'] = 'INTERRUPTED'
        try:
            if browser_output.exists():
                shutil.copytree(browser_output, output / 'browser')
        except Exception as error:
            report.update(status='FAIL', artifact_error=str(error))
            raise
        finally:
            write_json(output / 'report.json', report)


def run_additional_cases(output, env):
    output.mkdir(parents=True, exist_ok=False)
    suites = (
        ('agent-draft', 'agent-draft-state.e2e.js', DRAFT_CASES, ('agent_console.html', 'agent_console.mjs')),
        ('console-scope', 'console-scope-state.e2e.js', SCOPE_CASES, ('console.html', 'console.mjs')),
    )
    report = {'status': 'PASS', 'suites': {}, 'new_model_calls': None, 'usage_observed': False,
              'zero_model_usage_verified': False}
    for name, tests, expected, sources in suites:
        try:
            detail = run_cases(output / name, env, tests=tests, expected=expected, sources=sources)
            report['suites'][name] = {'status': 'PASS', **detail}
        except Exception as error:
            report['status'] = 'FAIL'
            detail = {'usage_observed': False, 'usage_observation': 'NOT_RUN',
                      'browser_usage': None, 'new_model_calls': None, 'zero_model_usage_verified': False}
            path = output / name / 'report.json'
            if path.exists():
                try:
                    detail = json.loads(path.read_text('utf-8'))
                    if not isinstance(detail, dict):
                        raise ValueError('Invalid browser suite observation')
                except (OSError, ValueError):
                    detail = {'usage_observed': False, 'usage_observation': 'UNKNOWN',
                              'browser_usage': None, 'new_model_calls': None, 'zero_model_usage_verified': False}
            report['suites'][name] = {**detail, 'status': 'FAIL', 'error': str(error)}
    report['usage_observed'] = all(row.get('usage_observed') is True for row in report['suites'].values())
    report['zero_model_usage_verified'] = report['usage_observed'] and all(
        row.get('zero_model_usage_verified') is True for row in report['suites'].values())
    report['new_model_calls'] = 0 if report['zero_model_usage_verified'] else None
    write_json(output / 'report.json', report)
    if report['status'] != 'PASS':
        raise RuntimeError(f'Additional browser state regressions failed; see {output / "report.json"}')
    return report



class StaticConsole(BaseHTTPRequestHandler):
    def do_GET(self):
        files = {'/': ('console.html', 'text/html'), '/console.mjs': ('console.mjs', 'text/javascript')}
        if self.path not in files:
            self.send_error(404)
            return
        name, media_type = files[self.path]
        content = (ROOT / 'kb_platform' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', media_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass


def run_static(output, chromium=None):
    output.mkdir(parents=True, exist_ok=False)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('KB_', 'OPENAI_', 'GOOGLE_', 'ANTHROPIC_', 'AZURE_'))}
    process, detail, failure, version = None, None, None, None
    server = ThreadingHTTPServer(('127.0.0.1', 0), StaticConsole)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env['KB_E2E_URL'] = f'http://127.0.0.1:{server.server_port}'
    try:
        with TemporaryDirectory(prefix='evidence-console-browser-') as temporary:
            try:
                if chromium is not None:
                    executable = chromium.resolve(strict=True)
                    version = subprocess.run([str(executable), '--version'], check=True, capture_output=True,
                                             text=True, timeout=10).stdout.strip()
                    with (output / 'chromium.log').open('w', encoding='utf-8') as log:
                        process = subprocess.Popen([str(executable), '--headless=new',
                            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                            '--user-data-dir=' + temporary, '--no-first-run', '--no-default-browser-check',
                            '--disable-background-networking', 'about:blank'], stdout=log, stderr=subprocess.STDOUT)
                    active = Path(temporary) / 'DevToolsActivePort'
                    deadline = time.monotonic() + 15
                    while not active.is_file():
                        if process.poll() is not None:
                            raise RuntimeError('Owned Chromium exited before CDP readiness; no sandbox fallback attempted')
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Owned Chromium did not become ready')
                        time.sleep(0.05)
                    port = int(active.read_text('utf-8').splitlines()[0])
                    if not 1 <= port <= 65535:
                        raise ValueError('Invalid owned Chromium CDP port')
                    env['KB_E2E_CDP_ENDPOINT'] = f'http://127.0.0.1:{port}'
                detail = run_cases(output / 'console-state', env)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    except Exception as error:
        failure = str(error)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    report = {'decision': 'FAIL' if failure else 'PASS', 'error': failure, 'checks': detail,
        'browser_version': version, 'source_sha256': {
            name: sha256((ROOT / 'kb_platform' / name).read_bytes()).hexdigest()
            for name in ('console.html', 'console.mjs')},
        'scope': 'Owned loopback static console, actual browser DOM, synthetic fetch scheduling; no product API or provider',
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0}
    write_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Run controlled console browser regressions without a product server.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', type=Path, help='Explicit already-installed Chromium, owned temporary profile and loopback CDP')
    args = parser.parse_args()
    report = run_static(args.output.resolve(), args.chromium)
    print(json.dumps(report))
    return int(report['decision'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())

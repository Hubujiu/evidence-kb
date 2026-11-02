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

from .browser_integration import browser_verdict
from .runner import write_json


ROOT = Path(__file__).resolve().parents[2]
CASES = frozenset(name + ' [chromium]' for name in (
    'newer citation survives older metadata completion',
    'late preview body cannot mix citations or revoke the current URL',
    'stale citation fetch failure does not replace the newer view status',
    'scope switch rejects late error-body status and successful preview body',
    'current errors remain visible and scope reset releases its preview URL once',
))


def run_cases(output, env):
    output.mkdir(parents=True, exist_ok=False)
    node = shutil.which('node')
    if node is None:
        raise ValueError('Node is required for controlled console browser checks')
    command = [node, str(ROOT / 'e2e/node_modules/e2e/dist/cli/bin.js'), 'run',
               '--output', str(output / 'browser'), '--no-cache', '--retries', '0']
    with (output / 'browser.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, cwd=ROOT / 'e2e', env={**env,
            'KB_E2E_TESTS': 'console-state.e2e.js', 'E2E_TELEMETRY_DISABLED': '1'},
            stdout=log, stderr=subprocess.STDOUT, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError(f'Console browser regression failed with exit {result.returncode}; see {output / "browser.log"}')
    report = browser_verdict(output / 'browser/junit.xml', expected=CASES, classname='console-state.e2e.js')
    run = json.loads((output / 'browser/report.json').read_text('utf-8'))['run']
    if (run['status'] != 'passed' or run['errors'] or run['usage']['modelTokens'] != 0
            or run['usage']['maxModelCallsInStep'] != 0):
        raise RuntimeError('Console browser checks require every deterministic case and zero model calls')
    return {**report, 'browser_usage': run['usage'], 'source_sha256': {
        name: sha256((ROOT / 'kb_platform' / name).read_bytes()).hexdigest()
        for name in ('console.html', 'console.mjs')}}


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
            try:
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
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
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

import argparse
import hashlib
import json
import os
import select
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORT = 58089


def required_file(value):
    path = (ROOT / value).resolve()
    if not path.is_file():
        raise ValueError(f'Missing required file: {path}')
    return path


def process_start(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]


def write_receipt(path, receipt):
    fd, name = tempfile.mkstemp(prefix='.process-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def start(args):
    python = ROOT / '.venv/bin/python'
    if not python.is_file():
        raise ValueError('Missing project .venv Python.')
    config = required_file(args.config)
    providers = required_file(args.providers)
    semantic = required_file(args.semantic_runtime)
    run_root = (ROOT / args.run_root).resolve()
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['KB_SEMANTIC_PAGE_CONFIG'] = str(semantic)
    env.pop('KB_DELIVERY_BUDGET_BATCH', None)
    if args.budget_batch:
        env['KB_DELIVERY_BUDGET_BATCH'] = str(required_file(args.budget_batch))
    check = [str(python), '-B', '-m', 'scripts.delivery_server_app', '--check', '--config', str(config),
             '--providers', str(providers), '--run-root', str(run_root)]
    if subprocess.run(check, cwd=ROOT, env=env, check=False).returncode:
        raise ValueError('Delivery configuration validation failed; no service started.')
    if args.check_only:
        return
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('Delivery startup requires Linux pidfd support.')
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL or threading.active_count() != 1:
        raise ValueError('Delivery startup requires exclusive child process ownership.')
    probe = os.pidfd_open(os.getpid())
    os.close(probe)
    host = required_file(args.host_binary or 'target/release/evidence-host')
    kernel = required_file(host.parent / 'libagent_kernel.so')
    env['KB_AGENT_KERNEL_LIBRARY'] = str(kernel)
    profile = json.loads(config.read_text(encoding='utf-8'))['id']
    with socket.socket() as listener:
        try:
            listener.bind(('127.0.0.1', PORT))
        except OSError as exc:
            raise ValueError(f'Port {PORT} is occupied. No existing process was stopped.') from exc
    run = run_root / f"server-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}"
    run.mkdir(mode=0o700, parents=True)
    if args.start_dependencies:
        with (run / 'dependencies.stdout.log').open('wb') as out, (run / 'dependencies.stderr.log').open('wb') as err:
            dependency = subprocess.Popen(['docker', 'compose', 'up', '-d', '--wait', 'postgres'], cwd=ROOT, env=env,
                                          stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            try:
                code = dependency.wait(timeout=180)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(f'Dependency command still running; owned PID {dependency.pid}. Inspect {run}.') from exc
            if code:
                raise ValueError(f'Dependency startup failed. Inspect {run}.')
    instance = secrets.token_hex(32)
    env.update(KB_DELIVERY_CONFIG=str(config), KB_DELIVERY_PROVIDERS=str(providers), KB_DELIVERY_ROOT=str(run_root),
               KB_DELIVERY_INSTANCE=instance)
    stop_file = run / 'stop.request'
    host_profile = ROOT / 'config/host-profiles/delivery.json'
    read_fd, write_fd = os.pipe()
    launch_code = ('import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); '
                   'os.execv(sys.argv[2],sys.argv[2:]) if ready == b"1" else sys.exit(125)')
    try:
        with (run / 'stdout.log').open('wb') as out, (run / 'stderr.log').open('wb') as err:
            process = subprocess.Popen([str(python), '-I', '-S', '-B', '-c', launch_code, str(read_fd), str(host),
                                        '--profile', str(host_profile), '--worker', str(python), '--stop-file',
                                        str(stop_file)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=True, pass_fds=(read_fd,))
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    receipt_path = run / 'process.json'
    pidfd = None
    published = False
    try:
        pidfd = os.pidfd_open(process.pid)
        receipt = {'pid': process.pid, 'process_start': process_start(process.pid), 'port': PORT, 'run': str(run),
                   'profile': profile, 'host_binary': str(host), 'host_profile': str(host_profile),
                   'stop_file': str(stop_file), 'config': str(config), 'providers': str(providers),
                   'identities': str(run_root / 'identities.json'), 'semantic_runtime': str(semantic),
                   'semantic_runtime_sha256': hashlib.sha256(semantic.read_bytes()).hexdigest(),
                   'budget_batch': env.get('KB_DELIVERY_BUDGET_BATCH'), 'started_at': datetime.now(timezone.utc).isoformat(),
                   'status': 'STARTING', 'token_printed': False}
        write_receipt(receipt_path, receipt)
        published = True
        os.write(write_fd, b'1')
    except BaseException as exc:
        if published:
            raise RuntimeError(f'Host PID {process.pid} may be running; inspect receipt {receipt_path}.') from exc
        try:
            try:
                if pidfd is None:
                    process.kill()
                else:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as cleanup_error:
            raise RuntimeError(f'Host rollback failed; PID {process.pid} requires inspection in {run}.') from cleanup_error
        raise
    finally:
        os.close(write_fd)
        if pidfd is not None:
            os.close(pidfd)
    for _ in range(20):
        if process.poll() is not None:
            raise ValueError(f'Owned service exited. Inspect {run / "stderr.log"}.')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=2) as response:
                if (response.status == 200 and response.headers.get('X-Delivery-Profile') == profile
                        and response.headers.get('X-Delivery-Instance') == instance):
                    receipt['status'] = 'RUNNING_LIVENESS_ONLY'
                    try:
                        write_receipt(receipt_path, receipt)
                    except OSError as exc:
                        raise RuntimeError(f'Host PID {process.pid} remains running; inspect receipt {receipt_path}.') from exc
                    print(f'Delivery process running: PID {process.pid}, http://127.0.0.1:{PORT}. '
                          f'Check authenticated dependency readiness in the conversation page. '
                          f'Private identity: {run_root / "identities.json"}. Receipt: {receipt_path}')
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise ValueError(f'Owned service did not confirm readiness yet; PID {process.pid}, logs {run}. No process was stopped.')


def stop(args):
    receipt_path = Path(args.receipt).resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    run = Path(receipt['run']).resolve(strict=True)
    marker = Path(receipt['stop_file'])
    if receipt_path.parent != run or marker.parent.resolve(strict=True) != run or marker.name != 'stop.request':
        raise ValueError('Stop marker must belong to the recorded server run.')
    pid = receipt['pid']
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        print('Recorded host already exited.')
        return
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if poller.poll(0):
            print('Recorded host already exited.')
            return
        actual_start = process_start(pid)
        executable = Path(f'/proc/{pid}/exe').resolve(strict=True)
        if actual_start != receipt['process_start'] or executable != Path(receipt['host_binary']).resolve(strict=True):
            raise ValueError('Recorded PID belongs to a different process.')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(marker, flags, 0o600)
        except FileExistsError:
            marker_info = marker.lstat()
            if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink != 1:
                raise ValueError('Existing stop marker is not a private regular file.') from None
        else:
            try:
                os.write(fd, b'stop')
            finally:
                os.close(fd)
        if not poller.poll(args.timeout_seconds * 1000):
            raise ValueError('Host has not exited. Inspect the run logs; no process was forcibly terminated.')
    finally:
        os.close(pidfd)
    print(f'Rust host exited. Inspect {run / "stdout.log"} and stderr.log for cleanup status.')


def main():
    parser = argparse.ArgumentParser(description='Start or stop the local delivery service on Linux.')
    commands = parser.add_subparsers(dest='command', required=True)
    launch = commands.add_parser('start')
    launch.add_argument('--config', default='config/delivery-local-v1.json')
    launch.add_argument('--providers', default='.local/delivery-20261026-01/providers.json')
    launch.add_argument('--run-root', default='.local/delivery-20261026-01')
    launch.add_argument('--semantic-runtime', default='.local/delivery-20261026-01/postgres-semantic-runtime.json')
    launch.add_argument('--budget-batch', default='')
    launch.add_argument('--host-binary', default='')
    launch.add_argument('--check-only', action='store_true')
    launch.add_argument('--start-dependencies', action='store_true')
    terminate = commands.add_parser('stop')
    terminate.add_argument('--receipt', required=True)
    terminate.add_argument('--timeout-seconds', type=int, default=30)
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Delivery control requires Linux.')
    if args.command == 'stop' and args.timeout_seconds < 1:
        parser.error('--timeout-seconds must be positive')
    try:
        start(args) if args.command == 'start' else stop(args)
    except (ValueError, OSError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()

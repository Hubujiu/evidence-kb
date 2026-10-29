from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter_ns


ROOT = Path(__file__).resolve().parents[2]
CATALOG_SIZES = (1, 32, 128, 256)
SCOPE = ('synthetic local plugin dispatch through the real Python/JSON/Rust ABI; '
         'excludes network, model inference, retrieval, persistence and provider latency')


def percentile(values, fraction):

    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _summary(durations):
    return {'p50_us': percentile(durations, 0.50) / 1000 if durations else None,
            'p95_us': percentile(durations, 0.95) / 1000 if durations else None}


def _measure(operation, validate, *, samples, warmup):
    errors, warmup_errors, durations = Counter(), Counter(), []
    for index in range(warmup + samples):
        start = perf_counter_ns()
        try:
            result = operation(index)
            elapsed = perf_counter_ns() - start
            validate(result, index)
        except Exception as error:
            elapsed = perf_counter_ns() - start
            (warmup_errors if index < warmup else errors)[type(error).__name__] += 1
        if index >= warmup:

            durations.append(elapsed)
    return {'samples': samples, 'warmup': warmup, 'errors': dict(errors),
            'warmup_errors': dict(warmup_errors), 'durations_ns': durations,
            **_summary(durations)}


def _rss():
    try:
        import resource
    except ImportError:
        return {'process_peak_rss_bytes': None, 'rss_source': 'unavailable'}
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {'process_peak_rss_bytes': int(peak if sys.platform == 'darwin' else peak * 1024),
            'rss_source': 'getrusage(RUSAGE_SELF).ru_maxrss; process cumulative peak'}


def _deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg',
                 'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'}:
        raise RuntimeError('OFFLINE_BENCHMARK_FORBIDS_' + event)


def _check_equal(result, expected):
    if result != expected:
        raise ValueError('BENCHMARK_RESULT_MISMATCH')


def _worker(args):


    sys.addaudithook(_deny_network)
    from kb_platform.agent_plugins import ToolContext, ToolPlugin, ToolResult
    from kb_platform.harness_services import builtin_plugins
    from kb_platform.plugin_host import PluginHost, PluginSpec

    class Echo:
        def increment(self, value):
            return value + 1

    def spec(index):
        name = f'fixture.service.{index:04d}'
        return PluginSpec(name, (name,), (), lambda ctx, config: ctx.provide(name, Echo()))

    rows = []
    for size in CATALOG_SIZES:
        catalog = tuple(spec(index) for index in range(size))
        settings = {'version': 1, 'plugins': [{'id': item.key, 'use': item.key} for item in catalog]}
        target = catalog[-1].key
        with PluginHost(settings, catalog) as host:
            row = _measure(lambda index: host.call(target, 'increment', index),
                           lambda result, index: _check_equal(result, index + 1),
                           samples=args.samples, warmup=args.warmup)
            rows.append({'workload': 'hot_call_last_service', 'catalog_size': size,
                         'target': target, **row, **_rss()})

            def scoped_call(index):
                with host.scope({'version': 1, 'plugins': []}) as child:
                    return child.call(target, 'increment', index)

            row = _measure(scoped_call, lambda result, index: _check_equal(result, index + 1),
                           samples=args.scope_samples, warmup=args.warmup)
            rows.append({'workload': 'empty_scope_create_inherited_call_close', 'catalog_size': size,
                         'target': target, **row, **_rss()})

    settings = {'version': 1, 'plugins': [
        {'id': 'runtime', 'use': 'harness.runtime'},
        {'id': 'sessions', 'use': 'harness.sessions.memory'},
        {'id': 'tools', 'use': 'harness.tools'},
        {'id': 'loop', 'use': 'harness.loop'},
    ]}
    tool = ToolPlugin('echo', 'Synthetic local echo.',
                      {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                       'required': ['value'], 'additionalProperties': False},
                      lambda context, arguments: ToolResult(arguments))
    with PluginHost(settings, builtin_plugins()) as host:
        def single_tool(index):
            context = ToolContext('synthetic', 'benchmark-session', f'turn-{index}', call_id=f'call-{index}')
            return host.call('loop', 'invoke', tool, context, {'value': index})

        row = _measure(single_tool, lambda result, index: _check_equal(result, {'value': index}),
                       samples=args.loop_samples, warmup=args.warmup)
        rows.append({'workload': 'single_tool_turn', 'catalog_size': 4, **row, **_rss()})
    return {'pid': os.getpid(), 'rows': rows, **_rss(), 'new_provider_calls': 0,
            'network_guard': True, 'scope': SCOPE}


def _command(*args):
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _metadata(args):
    paths = ['kb_platform/plugin_host.py', 'kb_platform/plugin_native.py',
             'kb_platform/harness_loop.py', 'rust/agent-harness/src/host.rs',
             'rust/agent-harness/src/agent_loop.rs', 'Cargo.lock']
    cpu = platform.processor()
    cpuinfo = Path('/proc/cpuinfo')
    if cpuinfo.is_file():
        cpu = next((line.split(':', 1)[1].strip() for line in cpuinfo.read_text().splitlines()
                    if line.startswith('model name')), cpu)
    return {'scope': SCOPE, 'python': sys.version, 'executable': sys.executable,
            'platform': platform.platform(), 'machine': platform.machine(), 'cpu': cpu,
            'logical_cpus': os.cpu_count(), 'git_commit': _command('git', 'rev-parse', 'HEAD'),
            'git_status': _command('git', 'status', '--short'), 'rustc': _command('rustc', '--version'),
            'build_profile_declared': args.build_profile, 'library': str(args.library),
            'library_sha256': sha256(args.library.read_bytes()).hexdigest(),
            'benchmark_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_sha256': {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths},
            'plan': {'catalog_sizes': list(CATALOG_SIZES), 'processes': args.processes,
                     'samples': args.samples, 'scope_samples': args.scope_samples,
                     'loop_samples': args.loop_samples, 'warmup_per_workload': args.warmup},
            'measurement': {'unit': 'one complete invocation', 'quantile': 'nearest rank',
                            'warmup_excluded': True, 'failed_samples_excluded': False,
                            'catalog_setup_in_hot_call': False, 'result_validation_in_latency': False,
                            'hot_target': 'last registered service; synthetic scan-sensitive workload'}}


def _aggregate(workers):
    grouped = {}
    for worker in workers:
        for row in worker['rows']:
            key = (row['workload'], row['catalog_size'])
            grouped.setdefault(key, []).append(row)
    result = []
    for (workload, size), rows in grouped.items():
        durations = [value for row in rows for value in row['durations_ns']]
        errors = Counter()
        for row in rows:
            errors.update(row['errors'])
            errors.update(row['warmup_errors'])
        result.append({'workload': workload, 'catalog_size': size, 'processes': len(rows),
                       'samples': len(durations), 'errors_including_warmup': dict(errors),
                       **_summary(durations),
                       'process_quantiles': [{key: row[key] for key in ('p50_us', 'p95_us')} for row in rows],
                       'process_peak_rss_bytes': [row['process_peak_rss_bytes'] for row in rows]})
    return result


def main():
    parser = argparse.ArgumentParser(description='Offline synthetic Rust plugin/ABI microbenchmark, not model or RAG latency.\n\nRun this same module in each checkout with separately built release libraries.\nEvery repetition starts a fresh process. Outputs preserve failed observations;\nno latency threshold is used as a correctness gate.\n')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--build-profile', choices=('release', 'debug'), required=True)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--scope-samples', type=int, default=100)
    parser.add_argument('--loop-samples', type=int, default=100)
    parser.add_argument('--warmup', type=int, default=20)
    parser.add_argument('--processes', type=int, default=5)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(getattr(args, key) < 1 for key in ('samples', 'scope_samples', 'loop_samples', 'processes')):
        parser.error('sample and process counts must be positive')
    if args.warmup < 0:
        parser.error('warmup must be nonnegative')
    args.library = args.library.resolve()
    if not args.library.is_file():
        parser.error('--library must identify an already built native library')
    os.environ.update({'KB_AGENT_KERNEL_LIBRARY': str(args.library), 'KB_DEFER_DEFAULT_APP': '1',
                       'KB_REMOTE_MODELS': '0', 'KB_ENABLE_EMBEDDINGS': '0'})
    if args.worker:
        report = _worker(args)
        print(json.dumps(report))
        return int(any(row['errors'] or row['warmup_errors'] for row in report['rows']))
    if args.output is None:
        parser.error('--output is required and must be a new directory')
    output = args.output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error('output already exists; previous measurements are read-only')
    metadata = _metadata(args)
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    workers, failures = [], []
    started = perf_counter_ns()
    for index in range(args.processes):
        command = [sys.executable, '-m', 'kb_platform.rag_testing.harness_performance', '--worker',
                   '--library', str(args.library), '--build-profile', args.build_profile,
                   '--samples', str(args.samples), '--scope-samples', str(args.scope_samples),
                   '--loop-samples', str(args.loop_samples), '--warmup', str(args.warmup)]
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                       timeout=30, check=False)
            (output / f'process-{index}.stderr.log').write_text(completed.stderr, encoding='utf-8')
            (output / f'process-{index}.json').write_text(completed.stdout, encoding='utf-8')
            workers.append(json.loads(completed.stdout))
            if completed.returncode:
                failures.append({'process': index, 'exit_code': completed.returncode})
        except (subprocess.TimeoutExpired, ValueError, OSError) as error:
            failures.append({'process': index, 'error': type(error).__name__})
    report = {'decision': 'FAIL' if failures else 'PASS', 'scope': SCOPE,
              'planned_processes': args.processes, 'completed_processes': len(workers),
              'failures': failures, 'wall_seconds': (perf_counter_ns() - started) / 1e9,
              'rows': _aggregate(workers), 'new_provider_calls': 0}
    (output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'decision': report['decision'], 'summary': str(output / 'summary.json'),
                      'wall_seconds': report['wall_seconds']}))
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())

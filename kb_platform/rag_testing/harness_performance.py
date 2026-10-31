from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import ctypes
import gc
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
from tempfile import TemporaryDirectory
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
    if sys.platform == 'win32':
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        *((name, ctypes.c_size_t) for name in (
                            'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                            'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                            'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage'))]

        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        read = ctypes.windll.psapi.GetProcessMemoryInfo
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        read.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not read(current(), ctypes.byref(counters), counters.cb):
            raise OSError('PROCESS_MEMORY_COUNTERS_UNAVAILABLE')
        handles = wintypes.DWORD()
        count_handles = ctypes.windll.kernel32.GetProcessHandleCount
        count_handles.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        count_handles.restype = wintypes.BOOL
        if not count_handles(current(), ctypes.byref(handles)):
            raise OSError('PROCESS_HANDLE_COUNT_UNAVAILABLE')
        return {'process_peak_rss_bytes': None, 'current_process_handles': handles.value,
                'current_python_threads': threading.active_count(), 'current_rss_bytes': counters.WorkingSetSize,
                'current_private_bytes': counters.PrivateUsage,
                'rss_source': 'Windows GetProcessMemoryInfo current WorkingSetSize and PrivateUsage'}
    status = Path('/proc/self/status')
    if status.is_file():
        values = dict(line.split(':', 1) for line in status.read_text().splitlines() if ':' in line)
        private = Path('/proc/self/smaps_rollup')
        private_bytes = None
        if private.is_file():
            private_bytes = sum(int(line.split()[1]) * 1024 for line in private.read_text().splitlines()
                                if line.startswith(('Private_Clean:', 'Private_Dirty:')))
        return {'process_peak_rss_bytes': None, 'current_rss_bytes': int(values['VmRSS'].split()[0]) * 1024,
                'current_private_bytes': private_bytes, 'rss_source': '/proc/self/status and smaps_rollup current'}
    return {'process_peak_rss_bytes': None, 'current_rss_bytes': None, 'current_private_bytes': None,
            'rss_source': 'current memory unavailable; no cumulative peak substituted'}


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
    extended = _extended_worker(args) if args.extended else None
    return {'pid': os.getpid(), 'rows': rows, 'extended': extended, **_rss(), 'new_provider_calls': 0,
            'network_guard': True, 'scope': SCOPE}


def _residual(host):
    collected = getattr(host, '_collection', None)
    if collected is None:
        raise ValueError('RUNTIME_COLLECTION_OBSERVATION_MISSING')
    remaining = collected['remaining']
    if any(remaining.values()):
        raise ValueError('RUNTIME_RESOURCES_REMAIN_AFTER_COLLECTION')
    return remaining


def _extended_worker(args):
    from kb_platform.plugin_host import PluginHost, PluginSpec

    rows, residuals = [], []
    samples, warmup = args.extended_samples, min(args.warmup, 5)

    class Value:
        def __init__(self, value):
            self.value = value

        def increment(self, index):
            return self.value + index

    closed = Counter()

    def mount(ctx, config):
        ctx.defer(lambda: closed.update(resource=1))
        ctx.provide('value', Value(config.get('value', 0)))

    catalog = (PluginSpec('bench.value', ('value',), (), mount),)
    profile = {'version': 1, 'plugins': [{'id': 'value', 'use': 'bench.value'}]}

    def cold(index):
        with PluginHost(profile, catalog) as host:
            result = host.call('value', 'increment', index)
        residuals[:] = [_residual(host)]
        return result

    prepared = [PluginHost(profile, catalog) for _ in range(samples + warmup)]

    def stop(index):
        prepared[index].close()
        return prepared[index].inspect()['state']

    row = _measure(stop, lambda result, index: _check_equal(result, 'disposed'),
                   samples=samples, warmup=warmup)
    residuals[:] = [_residual(prepared[-1])]
    rows.append({'workload': 'new_bridge_idle_stop_and_collect', 'catalog_size': 1,
                 'latency_unit': 'one idle root stop, owned cleanup and collection; setup excluded', **row, **_rss()})
    prepared.clear()

    row = _measure(cold, _check_equal, samples=samples, warmup=warmup)
    rows.append({'workload': 'new_bridge_cold_create_call_dispose_collect', 'catalog_size': 1,
                 'latency_unit': 'one complete lifecycle', **row, **_rss()})

    def dependent(ctx, config):
        ctx.provide('dependent', ctx.require('value'))

    dependencies = (*catalog, PluginSpec('bench.consumer', ('dependent',), ('value',), dependent))
    dependent_profile = {'version': 1, 'plugins': [*profile['plugins'],
        {'id': 'consumer', 'use': 'bench.consumer'}]}
    with PluginHost(dependent_profile, dependencies) as host:
        def update(index):
            host.update('value', config={'value': index})
            return host.call('dependent', 'increment', index)

        row = _measure(update, lambda result, index: _check_equal(result, index * 2),
                       samples=samples, warmup=warmup)
        rows.append({'workload': 'new_bridge_dependency_replace_and_invoke', 'catalog_size': 2,
                     'latency_unit': 'one update including dependent reactivation and call', **row, **_rss()})
    residuals[:] = [_residual(host)]

    for mode in ('emit', 'parallel'):
        events = []

        def listener(ctx, config):
            for offset in range(4):
                ctx.on('benchmark.event', lambda value, offset=offset: events.append((value, offset)))

        spec = PluginSpec('bench.listeners', (), (), listener)
        with PluginHost({'version': 1, 'plugins': [{'id': 'listeners', 'use': spec.key}]}, (spec,)) as host:
            def event(index):
                before = len(events)
                host.dispatch('benchmark.event', index, mode=mode)
                return len(events) - before

            row = _measure(event, lambda result, index: _check_equal(result, 4),
                           samples=samples, warmup=warmup)
            rows.append({'workload': 'new_bridge_event_' + mode, 'catalog_size': 1, 'listeners': 4,
                         'latency_unit': 'one dispatch and all four callbacks', **row, **_rss()})
        residuals[:] = [_residual(host)]

    with PluginHost(profile, catalog) as host:
        for concurrency in (1, 4, 16):
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                def concurrent(index):
                    return list(pool.map(lambda value: host.call('value', 'increment', value),
                                         range(index, index + concurrency)))

                row = _measure(concurrent, lambda result, index: _check_equal(
                    result, list(range(index, index + concurrency))), samples=samples, warmup=warmup)
                rows.append({'workload': 'new_bridge_concurrent_calls', 'catalog_size': 1,
                             'concurrency': concurrency, 'latency_unit': f'batch of {concurrency} invocations',
                             'throughput_calls_per_second': samples * concurrency / (sum(row['durations_ns']) / 1e9),
                             **row, **_rss()})
    residuals[:] = [_residual(host)]

    from .default_path_contracts import ScriptedModel
    from .default_path_replay import replay_application
    from .product_fixture import Authority, Gateway

    authority = Authority([('Harbor Guide.md', 'Harbor Guide is valid only until 2030.')])
    gateway = Gateway(authority)
    gateway.authorize = lambda actor, scope: {'scope_id': scope, 'manifest_id': 'build', 'authority_revision': 1}
    gateway.sources = lambda actor, scope: [{'source_id': row['source_id'], 'doc_id': row['source_id'],
        'source_version_id': row['source_version_id'], 'title': row['filename']} for row in authority.rows]
    model_calls = []
    with TemporaryDirectory() as directory:
        def business(index):
            model = ScriptedModel(query='Harbor Guide')
            application, host = replay_application(authority, model, gateway, Path(directory) / str(index), None)
            with host:
                session = application.create(authority.actor, authority.scope)
                result = application.turn(authority.actor, session['session_id'],
                    message='What does Harbor Guide require?', request_id='fixed-request', expected_revision=0)
                persisted = application.get(authority.actor, session['session_id'])
                answer = next(call for call in model.calls if call['role'] == 'answer')
                evidence = json.loads(answer['messages'][-1]['content'])['evidence']
                if not any(item.get('text') == 'Harbor Guide is valid only until 2030.' for item in evidence):
                    raise ValueError('FINAL_MODEL_EVIDENCE_MISSING')
                if persisted['revision'] != 1 or len(model.calls) != 4:
                    raise ValueError('BUSINESS_WORK_DID_NOT_COMPLETE')
                model_calls.append(len(model.calls))
            residuals[:] = [_residual(host)]
            return result['status'], result['answer']

        row = _measure(business, lambda result, index: _check_equal(
            result, ('answered', 'Harbor Guide is valid only until 2030.')),
            samples=min(samples, 10), warmup=min(warmup, 2))
        rows.append({'workload': 'new_offline_business_default_graph_with_durable_commit', 'catalog_size': 19,
                     'latency_unit': 'cold composition, session create, complete default graph, durable commit, cleanup',
                     'synthetic_model_invocations': sum(model_calls), 'new_provider_calls': 0, **row, **_rss()})

    points = []
    for index in range(args.memory_cycles + 20):
        cold(index)
        if index >= 20 and (index - 19) % 20 == 0:
            gc.collect()
            points.append({'completed_cycles': index - 19, 'resource_residuals': residuals[-1], **_rss()})
    return {'rows': rows, 'resource_residuals': residuals[-1], 'memory_cycles': args.memory_cycles,
            'memory_warmup_cycles': 20, 'memory_points': points,
            'memory_scope': 'fresh process current memory after repeated fully collected roots; allocator capacity retained',
            'closed_resource_callbacks': closed['resource'], 'synthetic_model_calls': sum(model_calls),
            'new_provider_calls': 0, 'comparison_scope': 'new characterization; no paired legacy workload exists'}


def _command(*args):
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _metadata(args):
    paths = ['kb_platform/plugin_host.py', 'kb_platform/plugin_native.py',
             'kb_platform/harness_loop.py', 'rust/agent-harness/src/agent_loop.rs', 'Cargo.lock']
    paths.extend(str(path.relative_to(ROOT)).replace('\\', '/') for path in
                 sorted((ROOT / 'rust/plugin-runtime').rglob('*.rs')))
    paths.extend(str(path.relative_to(ROOT)).replace('\\', '/') for path in
                 sorted((ROOT / 'kb_platform').rglob('*.py')))
    paths.extend(str(path.relative_to(ROOT)).replace('\\', '/') for path in
                 sorted((ROOT / 'config').rglob('*.json')))
    paths = sorted({path for path in paths if (ROOT / path).is_file()})
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
                     'loop_samples': args.loop_samples, 'warmup_per_workload': args.warmup,
                     'extended': args.extended, 'extended_samples': args.extended_samples,
                     'memory_cycles': args.memory_cycles},
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
                       'process_peak_rss_bytes': [row.get('process_peak_rss_bytes') for row in rows]})
    return result


LEGACY_BUDGETS_US = {
    ('hot_call_last_service', 1): 953.4,
    ('hot_call_last_service', 32): 1073.3,
    ('hot_call_last_service', 128): 758.2,
    ('hot_call_last_service', 256): 614.2,
    ('empty_scope_create_inherited_call_close', 1): 1997.6,
    ('empty_scope_create_inherited_call_close', 32): 2348.3,
    ('empty_scope_create_inherited_call_close', 128): 2091.8,
    ('empty_scope_create_inherited_call_close', 256): 1989.8,
    ('single_tool_turn', 4): 4469.3,
}


def _comparison(baseline, current):
    older = {(row['workload'], row['catalog_size']): row for row in _aggregate(baseline)}
    rows = []
    for row in _aggregate(current):
        key = row['workload'], row['catalog_size']
        before = older.get(key)
        current_values = [item['p95_us'] for item in row['process_quantiles']]
        baseline_values = [item['p95_us'] for item in before['process_quantiles']] if before else []
        disjoint = bool(baseline_values and (max(current_values) < min(baseline_values)
                                            or min(current_values) > max(baseline_values)))
        verdict = ('measurably_faster' if max(current_values) < min(baseline_values)
                   else 'measurably_slower') if disjoint else 'no_resolvable_difference'
        rows.append({'workload': key[0], 'catalog_size': key[1],
            'current_p95_median_us': statistics.median(current_values),
            'current_p95_range_us': [min(current_values), max(current_values)],
            'baseline_p95_median_us': statistics.median(baseline_values) if baseline_values else None,
            'baseline_p95_range_us': [min(baseline_values), max(baseline_values)] if baseline_values else None,
            'verdict': verdict if baseline_values else 'not_paired',
            'fixed_absolute_p95_budget_us': LEGACY_BUDGETS_US[key],
            'budget_statistic': 'maximum of independent process p95 values',
            'budget_pass': max(current_values) <= LEGACY_BUDGETS_US[key],
            'errors_including_warmup': row['errors_including_warmup']})
    return rows


def _run_worker(args, output, index, *, source, library, label, extended):
    command = [sys.executable, '-m', 'kb_platform.rag_testing.harness_performance', '--worker',
               '--library', str(library), '--build-profile', args.build_profile,
               '--samples', str(args.samples), '--scope-samples', str(args.scope_samples),
               '--loop-samples', str(args.loop_samples), '--warmup', str(args.warmup)]
    if extended:
        command.extend(['--extended', '--extended-samples', str(args.extended_samples),
                        '--memory-cycles', str(args.memory_cycles)])
    try:
        completed = subprocess.run(command, cwd=source, capture_output=True, text=True,
                                   timeout=120, check=False)
        stem = output / f'{label}-process-{index}'
        stem.with_suffix('.stderr.log').write_text(completed.stderr, encoding='utf-8')
        stem.with_suffix('.json').write_text(completed.stdout, encoding='utf-8')
        worker = json.loads(completed.stdout)
        if completed.returncode:
            return worker, {'side': label, 'process': index, 'exit_code': completed.returncode}
        return worker, None
    except (subprocess.TimeoutExpired, ValueError, OSError) as error:
        return None, {'side': label, 'process': index, 'error': type(error).__name__, 'detail': str(error)}


def _run_native(args, output):
    workers, failures = [], []
    if args.native_benchmark is None:
        return workers, failures
    for index in range(args.processes):
        try:
            completed = subprocess.run([str(args.native_benchmark), '--samples', str(args.samples)],
                cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
            (output / f'native-process-{index}.json').write_text(completed.stdout, encoding='utf-8')
            (output / f'native-process-{index}.stderr.log').write_text(completed.stderr, encoding='utf-8')
            worker = json.loads(completed.stdout)
            workers.append(worker)
            if completed.returncode or any(row['errors'] or row['warmup_errors'] or
                row['expected_callbacks'] != row['actual_callbacks'] or any(row['resource_residuals'].values())
                for row in worker['rows']):
                failures.append({'side': 'native', 'process': index, 'exit_code': completed.returncode})
        except (subprocess.TimeoutExpired, ValueError, OSError) as error:
            failures.append({'side': 'native', 'process': index, 'error': type(error).__name__})
    return workers, failures


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
    parser.add_argument('--extended', action='store_true')
    parser.add_argument('--extended-samples', type=int, default=40)
    parser.add_argument('--memory-cycles', type=int, default=200)
    parser.add_argument('--baseline-source', type=Path)
    parser.add_argument('--baseline-library', type=Path)
    parser.add_argument('--native-benchmark', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(getattr(args, key) < 1 for key in ('samples', 'scope_samples', 'loop_samples', 'processes',
                                             'extended_samples', 'memory_cycles')):
        parser.error('sample and process counts must be positive')
    if args.warmup < 0:
        parser.error('warmup must be nonnegative')
    if bool(args.baseline_source) != bool(args.baseline_library):
        parser.error('--baseline-source and --baseline-library must be supplied together')
    if args.baseline_source is not None:
        args.baseline_source = args.baseline_source.resolve()
        args.baseline_library = args.baseline_library.resolve()
        if not (args.baseline_source / 'kb_platform/rag_testing/harness_performance.py').is_file() or not args.baseline_library.is_file():
            parser.error('baseline must identify frozen source and release library')
    if args.native_benchmark is not None:
        args.native_benchmark = args.native_benchmark.resolve()
        if not args.native_benchmark.is_file():
            parser.error('--native-benchmark must identify an already built release executable')
    args.library = args.library.resolve()
    if not args.library.is_file():
        parser.error('--library must identify an already built native library')
    os.environ.update({'KB_AGENT_KERNEL_LIBRARY': str(args.library), 'KB_DEFER_DEFAULT_APP': '1',
                       'KB_REMOTE_MODELS': '0', 'KB_ENABLE_EMBEDDINGS': '0'})
    if args.worker:
        report = _worker(args)
        print(json.dumps(report))
        return int(any(row['errors'] or row['warmup_errors'] for row in
                       [*report['rows'], *(report['extended']['rows'] if report['extended'] else [])]))
    if args.output is None:
        parser.error('--output is required and must be a new directory')
    output = args.output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error('output already exists; previous measurements are read-only')
    metadata = _metadata(args)
    metadata['baseline'] = None if args.baseline_source is None else {
        'source': str(args.baseline_source), 'library': str(args.baseline_library),
        'library_sha256': sha256(args.baseline_library.read_bytes()).hexdigest(),
        'benchmark_sha256': sha256((args.baseline_source / 'kb_platform/rag_testing/harness_performance.py').read_bytes()).hexdigest()}
    metadata['native_benchmark'] = None if args.native_benchmark is None else {
        'path': str(args.native_benchmark), 'sha256': sha256(args.native_benchmark.read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    workers, baseline, failures = [], [], []
    started = perf_counter_ns()
    sequence = []
    for index in range(args.processes):
        if args.baseline_source is not None:
            worker, error = _run_worker(args, output, index, source=args.baseline_source,
                library=args.baseline_library, label='baseline', extended=False)
            sequence.append({'side': 'baseline', 'process': index})
            if worker is not None:
                baseline.append(worker)
            if error:
                failures.append(error)
        worker, error = _run_worker(args, output, index, source=ROOT,
            library=args.library, label='current', extended=False)
        sequence.append({'side': 'current', 'process': index})
        if worker is not None:
            workers.append(worker)
        if error:
            failures.append(error)
    extended = []
    if args.extended:
        for index in range(args.processes):
            worker, error = _run_worker(args, output, index, source=ROOT,
                library=args.library, label='extended', extended=True)
            if worker is not None:
                extended.append(worker['extended'])
            if error:
                failures.append(error)
    native, native_failures = _run_native(args, output)
    failures.extend(native_failures)
    comparison = _comparison(baseline, workers) if workers else []
    report = {'decision': 'FAIL' if failures else 'PASS', 'scope': SCOPE,
              'planned_processes': args.processes, 'completed_processes': len(workers),
              'paired_sequence': sequence, 'failures': failures,
              'wall_seconds': (perf_counter_ns() - started) / 1e9,
              'rows': _aggregate(workers), 'baseline_rows': _aggregate(baseline),
              'legacy_comparison': comparison,
              'frozen_budget_decision': 'PASS' if comparison and all(row['budget_pass'] for row in comparison) else 'FAIL',
              'extended_workers': extended, 'native_workers': native, 'new_provider_calls': 0,
              'memory_interpretation': 'current PrivateUsage and WorkingSetSize samples after collection; no peak-RSS per-kernel attribution',
              'performance_scope': 'offline synthetic components and business replay; no supplier or production RAG latency claim'}
    (output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'decision': report['decision'], 'summary': str(output / 'summary.json'),
                      'wall_seconds': report['wall_seconds']}))
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())

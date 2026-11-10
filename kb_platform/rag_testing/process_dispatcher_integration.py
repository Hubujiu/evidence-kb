from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import patch

from kb_platform import plugin_host


def verify_idle_exit_dispatcher(manager, installed, root):
    from . import plugin_process_integration as integration

    rows = []
    for release_before_wait in (True, False):
        entered, release = Event(), Event()
        operations, observed = set(), {}
        native, execute = plugin_host.native_call, plugin_host.PluginHost._execute
        original_wait, original_close = integration.wait_until, integration.close_when_quiescent

        def execution(host, action):
            if (action['kind'] == 'cleanup' and action['plugin'] == 'process'
                    and action.get('resource', 0) > 0):
                operations.add(action['operation'])
            return execute(host, action)

        def call(command, **fields):
            result = native(command, **fields)
            if command == 'runtime_complete' and fields['operation'] in operations:
                observed['before_release'] = native('runtime_inspect', host=fields['host'])
                entered.set()
                if not release.wait(10):
                    raise AssertionError('DISPATCHER_BARRIER_NOT_RELEASED')
            return result

        def wait(predicate, seconds):
            if seconds != 3 or not entered.wait(3):
                raise AssertionError('DISPATCHER_ORIGINAL_DEADLINE_OR_BARRIER')
            observed['early_predicate'] = bool(predicate())
            if release_before_wait:
                release.set()
            started = monotonic()
            value = original_wait(predicate, seconds)
            observed['wait_seconds'] = monotonic() - started
            return value

        def close(host):
            release.set()
            observed['settled_without_external_cleanup'] = original_wait(lambda: not any(
                host.inspect()['counts'][key] for key in
                ('leases', 'resources', 'operations', 'pending_operations', 'queued_actions')), 3)
            observed['after_release_before_close'] = host.inspect()
            original_close(host)

        directory = Path(root) / ('dispatcher-release' if release_before_wait else 'dispatcher-held')
        directory.mkdir()
        with patch.object(plugin_host.PluginHost, '_execute', execution), \
                patch.object(plugin_host, 'native_call', call), \
                patch.object(integration, 'wait_until', wait), \
                patch.object(integration, 'close_when_quiescent', close):
            try:
                outcome = integration.idle_exit_case(manager, installed, directory)
            finally:
                release.set()
        expected = 'PASS' if release_before_wait else 'FAIL'
        accepted = (outcome['decision'] == expected and not observed['early_predicate']
            and observed['before_release']['counts']['queued_actions'] == 1
            and observed['settled_without_external_cleanup']
            and not any(observed['after_release_before_close']['counts'][key] for key in
                        ('leases', 'resources', 'operations', 'pending_operations', 'queued_actions'))
            and (release_before_wait or observed['wait_seconds'] >= 3))
        rows.append({'mode': 'release-before-wait' if release_before_wait else 'hold-through-original-deadline',
            'decision': 'PASS' if accepted else 'FAIL', 'expected_observation': expected,
            'observation': outcome, **observed})
    return {'decision': 'PASS' if all(row['decision'] == 'PASS' for row in rows) else 'FAIL',
            'cases': rows, 'scope': 'Controlled original dispatcher observation boundary; no process retry, '
            'no external cleanup before settled observation, unchanged original three-second deadline.'}

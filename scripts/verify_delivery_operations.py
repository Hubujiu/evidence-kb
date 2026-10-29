"""Read-only, zero-model operational acceptance for an explicitly started runtime.

Only GET liveness/identity/readiness/status/list/export/UI requests are sent.
Private tokens, session IDs, transcripts, provider values and file paths never
enter the public report. This is not an answer-quality evaluation.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kb_platform.rag_testing.budget_batches import cumulative_summary  # noqa: E402
from kb_platform.provider_admission import ProviderAdmission  # noqa: E402


CHECKS = ('liveness', 'authenticated_identity', 'dependency_readiness', 'runtime_status',
          'own_session_list', 'authorized_session_export', 'bad_token_runtime', 'bad_token_sessions',
          'bad_token_export', 'bad_token_readiness', 'agent_ui_controls', 'single_default_profile',
          'runtime_ledger_agreement', 'settled_budget', 'zero_new_physical_calls')


def _read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def _input_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _provider_controls(identities, tenant_id):
    """Read private runtime wiring/control receipts independently of HTTP status."""
    runtime = Path(identities).resolve().parent / 'runtime.json'
    if not runtime.exists():
        return None  # Historical deployments had no provider latch observation.
    metadata = _read(runtime)
    if metadata.get('id') != tenant_id:
        raise ValueError('PROVIDER_RUNTIME_IDENTITY_MISMATCH')
    channels, checkpoints = [], {}
    for reference in metadata.get('provider_admission', []):
        control = ProviderAdmission.from_directory(reference['control_directory'])
        if (control.scope_hash != reference['scope_hash'] or
                control.identity['provider_id'] != reference['provider_id']):
            raise ValueError('PROVIDER_CONTROL_IDENTITY_MISMATCH')
        value = control.snapshot(public=True)
        if value not in channels:
            channels.append(value)
        checkpoints[control.scope_hash] = control.snapshot()['checkpoint']
    return {'channels': sorted(channels, key=lambda value: json.dumps(value, sort_keys=True)),
            'checkpoints': checkpoints, 'runtime_metadata_sha256': _input_hash(runtime)}


def _public_budget(snapshot):
    parents = snapshot['predecessors']
    return {'status': snapshot['status'], 'prior_phase_count': len(parents),
            'phases': [{'role': f'prior_{number + 1}', 'caps': phase['caps'],
                        'summary': phase['summary'], 'rows_sha256': phase['snapshot_sha256']}
                       for number, phase in enumerate(parents)] + [
                           {'role': 'runtime', 'caps': snapshot['new_batch']['caps'],
                            'summary': snapshot['new_batch']['summary'],
                            'rows_sha256': snapshot['new_batch']['snapshot_sha256']}],
            'cumulative': snapshot['cumulative'], 'predecessor_drift_count': len(snapshot['predecessor_drift']),
            'duplicate_physical_id_count': len(snapshot['duplicate_physical_request_ids'])}


class UI(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.profiles, self.in_profile = set(), [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if tag == 'input' and attrs.get('id') == 'profile' and attrs.get('type') == 'hidden':
            self.profiles.append(attrs)
        if tag == 'select':
            self.in_profile = attrs.get('id') == 'profile'
        if tag == 'option' and self.in_profile:
            self.profiles.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'select':
            self.in_profile = False


def _export_summary(value):
    def forbidden(item):
        if isinstance(item, dict):
            return (bool(set(item) & {'trace', 'state', 'tool_call', 'tool_result', 'private_debug',
                                     'checkpoints', 'artifacts'}) or any(forbidden(v) for v in item.values()))
        return isinstance(item, list) and any(forbidden(v) for v in item)

    expected = {'schema', 'exported_at', 'session', 'config_name', 'events', 'authorization',
                'contains_private_provider_credentials'}
    if (not isinstance(value, dict) or set(value) != expected or
            value['schema'] != 'authorized-session-export/1' or
            value['contains_private_provider_credentials'] is not False or forbidden(value)):
        raise ValueError('EXPORT_SCHEMA_OR_PRIVATE_FIELDS')
    if set(value['session']) != {'session_id', 'scope_id', 'revision', 'created_at', 'updated_at'}:
        raise ValueError('EXPORT_STATE_OR_METADATA_FIELDS')
    events = value['events']
    if not isinstance(events, list) or not events:
        raise ValueError('EXPORT_TRANSCRIPT_MISSING')
    for event in events:
        if (set(event) - {'seq', 'kind', 'created', 'payload'} or
                event.get('kind') not in ('user', 'turn_completed')):
            raise ValueError('EXPORT_RAW_EVENT_OR_TOOL')
        allowed = ({'text'} if event['kind'] == 'user' else
                   {'status', 'answer', 'completion', 'unresolved', 'claims', 'evidence',
                    'user_statements', 'reason', 'stop_reason'})
        if not isinstance(event['payload'], dict) or set(event['payload']) - allowed:
            raise ValueError('EXPORT_DEBUG_OR_PRIVATE_STATE')
    return {'events': len(events), 'user_events': sum(e['kind'] == 'user' for e in events),
            'final_response_events': sum(e['kind'] == 'turn_completed' for e in events),
            'redacted_responses': sum(e['payload'].get('status') == 'redacted' for e in events),
            'raw_tools_debug_state_absent': True,
            'scope': 'Currently authorized public transcript; not raw backup or semantic correctness'}


def verify_operations(*, config, runtime_manifest, identities, session_state, output,
                      base_url='http://127.0.0.1:58089', transport=None):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    parsed = urlparse(base_url)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('EXPLICIT_LOOPBACK_SERVICE_REQUIRED')
    configuration, manifest, selected = _read(config), _read(runtime_manifest), _read(session_state)
    if (manifest.get('protocol') != 'delivery-runtime-budget-batch/1' or
            manifest.get('tenant_id') != configuration.get('id') or
            selected.get('status') != 'COMPLETE' or
            not re.fullmatch(r'[a-f0-9]{32}', selected.get('session_id', ''))):
        raise ValueError('FROZEN_RUNTIME_OR_COMPLETE_SESSION_REQUIRED')
    private = _read(identities)
    choices = [(token, actor) for token, actor in private.items() if isinstance(token, str) and
               actor == {'tenant_id': configuration['id'], 'principal_id': 'local'} and len(token) >= 32]
    if not choices:
        raise ValueError('MATCHING_PRIVATE_IDENTITY_REQUIRED')
    token, actor = choices[0]
    session_id = selected['session_id']
    ledger_path = Path(runtime_manifest).resolve().parent / 'physical-calls.sqlite3'
    before = cumulative_summary(manifest['predecessors'], ledger_path)
    provider_before = _provider_controls(identities, configuration['id'])
    checks = {name: {'status': 'NOT_RUN', 'http_status': None} for name in CHECKS}
    observations = {}

    def request(client, name, path, expected=200, *, bad_token=False, unauthenticated=False):
        headers = {} if unauthenticated else {'Authorization': 'Bearer ' + ('invalid' if bad_token else token)}
        try:
            response = client.get(path, headers=headers)
            checks[name] = {'status': 'PASS' if response.status_code == expected else 'FAIL',
                            'http_status': response.status_code, 'expected_http_status': expected}
            return response
        except httpx.HTTPError as exc:
            checks[name] = {'status': 'ERROR', 'http_status': None, 'error': type(exc).__name__}
            return None

    def validate(name, function):
        if checks[name]['status'] != 'PASS':
            return
        try:
            value = function()
            if value is False:
                raise ValueError('OPERATION_CONTRACT_MISMATCH')
        except (ValueError, KeyError, TypeError, AttributeError):
            checks[name]['status'] = 'FAIL'
            checks[name]['reason'] = 'RESPONSE_CONTRACT_MISMATCH'

    with httpx.Client(base_url=base_url, timeout=10, transport=transport,
                      trust_env=False, follow_redirects=False) as client:
        health = request(client, 'liveness', '/health', unauthenticated=True)
        validate('liveness', lambda: health.json().get('status') == 'running' and
                 health.headers.get('X-Delivery-Profile') == configuration['id'])
        # Do not disclose the private bearer to a different process occupying the port.
        if checks['liveness']['status'] == 'PASS':
            identity = request(client, 'authenticated_identity', '/identity')
            validate('authenticated_identity', lambda: identity.json() == actor)
            readiness = request(client, 'dependency_readiness', '/agent/readiness')
            validate('dependency_readiness', lambda: readiness.json()['ready'] is True and
                readiness.json()['model_calls'] == 0 and readiness.json()['dependencies']['postgres']['ready'] is True and
                any(readiness.json()['dependencies'].get(name, {}).get('ready') is True for name in ('search', 'opensearch')))
            if checks['dependency_readiness']['status'] == 'PASS':
                observations['dependency_readiness'] = {'postgres_ready': True, 'search_ready': True,
                    'quality_scope': 'Connectivity only; not answer quality or per-scope published channels'}
            status = request(client, 'runtime_status', '/agent/runtime-status')
            if checks['runtime_status']['status'] == 'PASS':
                try:
                    parsed_status = status.json()
                    if not isinstance(parsed_status, dict):
                        raise ValueError('RUNTIME_STATUS_NOT_AN_OBJECT')
                    observations['runtime_status'] = parsed_status
                except ValueError:
                    checks['runtime_status']['status'] = 'FAIL'
            sessions = request(client, 'own_session_list', '/agent/sessions?limit=100')
            validate('own_session_list', lambda: sessions.json()['source_text_included'] is False and
                all(set(row) == {'session_id', 'scope_id', 'revision', 'created_at', 'updated_at'}
                    for row in sessions.json()['sessions']) and
                any(row['session_id'] == session_id for row in sessions.json()['sessions']))
            if checks['own_session_list']['status'] == 'PASS':
                observations['session_list'] = {'visible_count': len(sessions.json()['sessions']),
                                               'selected_existing_session_visible': True}
            exported = request(client, 'authorized_session_export', '/agent/sessions/' + session_id + '/export')
            def check_export():
                value = exported.json()
                if (value['session']['session_id'] != session_id or
                        not exported.headers.get('Content-Disposition', '').startswith('attachment;')):
                    raise ValueError('WRONG_EXPORTED_SESSION')
                observations['export'] = {**_export_summary(value), 'response_sha256': sha256(exported.content).hexdigest()}
                return True
            validate('authorized_session_export', check_export)
            for name, path in (
                    ('bad_token_runtime', '/agent/runtime-status'), ('bad_token_sessions', '/agent/sessions'),
                    ('bad_token_export', '/agent/sessions/' + session_id + '/export'),
                    ('bad_token_readiness', '/agent/readiness')):
                request(client, name, path, expected=401, bad_token=True)
            ui_response = request(client, 'agent_ui_controls', '/agent', unauthenticated=True)
            def check_ui():
                page = UI()
                page.feed(ui_response.text)
                required = {'savedSessions', 'exportSession', 'runtimeStatus', 'refreshRuntime', 'profile'}
                selected_options = [option for option in page.profiles if 'selected' in option]
                default = selected_options[-1] if selected_options else page.profiles[0] if page.profiles else {}
                return (required <= page.ids and len(page.profiles) == 1 and
                    default.get('value') == 'recommended')
            validate('agent_ui_controls', check_ui)
            capability = request(client, 'single_default_profile', '/agent/capabilities')
            validate('single_default_profile', lambda: capability.json()['recommended'] == configuration['recommended'] and
                     set(capability.json()['configurations']) == {configuration['recommended']})

    after = cumulative_summary(manifest['predecessors'], ledger_path)
    provider_after = _provider_controls(identities, configuration['id'])
    public_before, public_after = _public_budget(before), _public_budget(after)
    stable = public_before == public_after
    checks['zero_new_physical_calls'] = {'status': 'PASS' if stable else 'FAIL', 'http_status': None,
                                        'ledger_snapshots_equal': stable}
    checks['settled_budget'] = {'status': 'PASS' if before['status'] == after['status'] == 'SETTLED'
                               and after['cumulative']['pending_or_unknown'] == 0 else 'FAIL', 'http_status': None}
    status_value = observations.pop('runtime_status', None)
    if status_value is not None:
        phase = before['new_batch']
        remaining = {key: max(0, phase['caps'][key] - used)
                     for key, used in phase['summary']['effective_usage_or_reserved'].items()}
        expected_status = ('BLOCKED' if before['status'] == 'BLOCKED' else 'BUDGET_EXHAUSTED' if
                           any(remaining[key] == 0 for key in ('model', 'input_tokens', 'output_tokens')) else 'AVAILABLE')
        provider_agreement = True
        if provider_before is not None and provider_before['channels']:
            channels = provider_before['channels']
            provider_blocked = any(value['state'] == 'BLOCKED' for value in channels)
            provider_agreement = (provider_before == provider_after and
                status_value.get('provider_blocked') is provider_blocked and
                status_value.get('budget_status') == expected_status and
                sorted(status_value.get('provider_channels', []), key=lambda value: json.dumps(value, sort_keys=True)) == channels)
            observations['provider_admission'] = {'channels': channels,
                'checkpoint_snapshots_equal': provider_before == provider_after,
                'checkpoint_sha256': sha256(json.dumps(provider_before['checkpoints'], sort_keys=True).encode()).hexdigest(),
                'provider_probe_performed': False,
                'scope': 'Independent local permission receipts; BLOCKED is not authentication recovery or dependency failure'}
            if provider_blocked:
                expected_status = 'BLOCKED'
        elif 'provider_channels' in status_value:
            provider_agreement = False  # Never trust a new API-only observation.
        agreement = (status_value.get('configured') is True and status_value.get('mode') == 'runtime_batch' and
            status_value.get('batch_id') == manifest['id'] and status_value.get('caps') == phase['caps'] and
            status_value.get('effective_usage_or_reserved') == phase['summary']['effective_usage_or_reserved'] and
            status_value.get('remaining') == remaining and status_value.get('status') == expected_status and
            status_value.get('pending_or_unknown') == before['cumulative']['pending_or_unknown'] and
            status_value.get('cumulative_effective_usage_or_reserved') == before['cumulative']['effective_usage_or_reserved'] and
            provider_agreement)
        checks['runtime_ledger_agreement'] = {'status': 'PASS' if agreement else 'FAIL', 'http_status': None}
    counts = {value: sum(check['status'] == value for check in checks.values())
              for value in ('PASS', 'FAIL', 'ERROR', 'NOT_RUN')}
    report = {'schema': 'delivery-zero-model-operations/1',
        'decision': 'OPERATIONS_PASS' if counts['PASS'] == len(CHECKS) else 'OPERATIONS_FAIL',
        'total': len(CHECKS), 'counts': counts, 'checks': checks, 'observations': observations,
        'ledger_before': public_before, 'ledger_after': public_after,
        'new_physical_calls_verified': 0 if stable else None,
        'request_scope': 'GET only; no conversation turns, ingestion, builds, retrieval, generation or judges',
        'quality_scope': 'Operational acceptance only; readiness and export validity do not establish answer correctness',
        'ui_scope': 'Served HTML structure/default options only; no browser interaction claim',
        'input_sha256': {'config': _input_hash(config), 'runtime_manifest': _input_hash(runtime_manifest),
                         'selected_existing_session_state': _input_hash(session_state),
                         'script': _input_hash(__file__)},
        'private_response_bodies_saved': False, 'credentials_saved': False}
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if any(secret in serialized for secret in (token, session_id)):
        raise ValueError('PUBLIC_REPORT_PRIVACY_CHECK_FAILED')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'report.json').write_text(serialized, 'utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/delivery-local-v1.json')
    parser.add_argument('--runtime-manifest', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--session-state', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:58089')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = verify_operations(config=args.config, runtime_manifest=args.runtime_manifest,
        identities=args.identities, session_state=args.session_state, base_url=args.base_url, output=args.output)
    print(json.dumps({key: report[key] for key in ('decision', 'total', 'counts', 'new_physical_calls_verified')}))
    return 0 if report['decision'] == 'OPERATIONS_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

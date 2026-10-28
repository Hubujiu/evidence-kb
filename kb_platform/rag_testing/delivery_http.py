"""Thin HTTP/source/capture adapter for the existing unified runner.

No answer labels enter this adapter. No provider budget, retry policy, semantic
scorer or replacement controller is implemented here. The product's metered
transport remains the authority for physical calls and unknown reservations.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from ..agent_context import canonical
from ..core import Actor
from .contracts import Fragment, Plan, Suite, Trace, digest
from .delivery_fixtures import bind_suite, load_delivery_fixtures
from .runner import run, write_json


VERSION = 'delivery-real-http-actual-payload/2'
LEGACY_VERSION = 'delivery-real-http-actual-payload/1'
POLICY = {'llm_context': True, 'embedding': True, 'rerank': True, 'ocr': False,
          'compile': False, 'allowed_providers': ['google_gemini', 'magpie_luna']}


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    write_json(temporary, value)
    temporary.replace(path)


def physical_records(directory):
    result = []
    root = Path(directory)
    if not root.exists():
        return result
    for path in sorted(root.glob('*.json')):
        record = json.loads(path.read_text('utf-8'))
        if (not isinstance(record, dict) or not isinstance(record.get('physical_request_id'), str) or
                not isinstance(record.get('payload'), dict) or not isinstance(record.get('context'), dict) or
                record.get('payload_sha256') != digest(record['payload'])):
            raise ValueError('INVALID_PHYSICAL_CAPTURE:' + path.name)
        result.append(record)
    identities = [record['physical_request_id'] for record in result]
    if len(set(identities)) != len(identities):
        raise ValueError('DUPLICATE_PHYSICAL_CAPTURE')
    return result


def require_no_pending(directory):
    pending = [record['physical_request_id'] for record in physical_records(directory)
               if record.get('status') not in ('COMPLETE', 'ERROR')]
    if pending:
        raise RuntimeError('UNRESOLVED_PHYSICAL_REQUESTS:' + ','.join(pending))


def read_authority(client, scope_id):
    response = client.get(f'/scopes/{scope_id}/agent-authority')
    if response.status_code != 200:
        raise ValueError('AUTHORITY_ENDPOINT_UNAVAILABLE:HTTP_' + str(response.status_code))
    try:
        authority = response.json()
    except ValueError:
        raise ValueError('AUTHORITY_ENDPOINT_INVALID_JSON') from None
    _validate_authority(authority, scope_id)
    return authority


def _validate_authority(authority, scope_id):
    if (not isinstance(authority, dict) or authority.get('scope_id') != scope_id or
            not isinstance(authority.get('manifest_id'), str) or not authority['manifest_id'] or
            type(authority.get('authority_revision')) is not int or authority['authority_revision'] < 0):
        raise ValueError('AUTHORITY_ATTESTATION_MISSING_OR_INVALID')


def _assert_authority(actual, expected):
    _validate_authority(actual, expected['scope_id'])
    if any(actual[key] != expected[key] for key in ('scope_id', 'manifest_id', 'authority_revision')):
        raise ValueError('FROZEN_AUTHORITY_OR_MANIFEST_MISMATCH')


def _bind_prepared_http(client, fixtures, root, *, platform, identity, data_scopes, context_budget, state, state_path):
    """Bind only after all tenant mutations; every bind observes one stable epoch."""
    actor = Actor(**identity)
    before = {split: read_authority(client, value['scope_id']) for split, value in data_scopes.items()}
    if len({value['authority_revision'] for value in before.values()}) != 1:
        raise ValueError('TENANT_AUTHORITY_CHANGED_DURING_BIND')
    prepared = {'protocol': VERSION, 'fixture_root': fixtures['root'], 'identity': identity,
        'context_budget': context_budget, 'splits': {}, 'planned_case_ids': state['planned_case_ids'],
        'authority_observation': 'real_http_before_and_after_all_readonly_bindings'}
    state['status'] = 'BINDING'
    _atomic_json(state_path, state)
    for split, data in data_scopes.items():
        scope, expected = data['scope_id'], before[split]
        if data['published'].get('manifest_id') != expected['manifest_id']:
            raise ValueError('PREPARED_PUBLISHED_MANIFEST_CHANGED')
        _assert_authority(read_authority(client, scope), expected)
        bound = bind_suite(platform, actor, scope, fixtures, data['ingested'], split=split, context_budget=context_budget)
        if any(case.input.acl_revision != str(expected['authority_revision']) for case in bound['suite'].cases):
            raise ValueError('BOUND_SUITE_AUTHORITY_MISMATCH')
        if any(binding.get('build_id') != expected['manifest_id'] for binding in bound['bindings'].values()):
            raise ValueError('BOUND_CANONICAL_MANIFEST_MISMATCH')
        _assert_authority(read_authority(client, scope), expected)
        files = {'suite': split + '-suite.json', 'bindings': split + '-bindings.json',
                 'expectations': split + '-expectations.json', 'documents': split + '-documents.json'}
        for kind, name in files.items():
            value = bound[kind].model_dump(mode='json') if kind == 'suite' else bound[kind]
            target = root / name
            if target.exists() and json.loads(target.read_text('utf-8')) != value:
                raise ValueError('BOUND_PREPARE_ARTIFACT_CHANGED:' + name)
            _atomic_json(target, value)
        prepared['splits'][split] = {**data, 'files': files, 'authority': expected}
    for split, expected in before.items():
        _assert_authority(read_authority(client, data_scopes[split]['scope_id']), expected)
    scenarios = json.loads((Path(fixtures['root']) / 'scenario-inputs.json').read_text('utf-8'))
    write_json(root / 'scenario-inputs.json', scenarios)
    prepared['scenario_inputs'] = 'scenario-inputs.json'
    prepared['file_sha256'] = {name: sha256((root / name).read_bytes()).hexdigest()
        for name in [*sum((list(item['files'].values()) for item in prepared['splits'].values()), []), 'scenario-inputs.json']}
    if 'rebind_from' in state:
        prepared.update(rebind_from=state['rebind_from'], reused_http_operation_receipts=len(state['operations']),
                        new_upload_build_publish_requests=0)
    _atomic_json(root / 'prepared.json', prepared)
    state['status'] = 'COMPLETE'
    _atomic_json(state_path, state)
    return prepared


def prepare_http(client, fixtures, output, *, platform, expected_tenant_id, physical_directory,
                 context_budget=118784, channels=('bm25', 'dense', 'hybrid'), policy=None):
    """Upload/build once through HTTP, then bind immutable originals read-only.

    Identity is configured by the serving process. A client cannot create or
    impersonate a tenant. The expected fresh tenant must be supplied explicitly.
    Existing operation receipts may be read; a STARTED/UNKNOWN or failed build
    is never automatically resubmitted. Publishing a completed build is safe.
    """
    fixtures = load_delivery_fixtures(fixtures['root'] if isinstance(fixtures, dict) else fixtures)
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'prepare-state.json'
    identity = client.get('/identity').raise_for_status().json()
    if identity.get('tenant_id') != expected_tenant_id:
        raise ValueError('DELIVERY_TENANT_MISMATCH')
    require_no_pending(physical_directory)
    specification = {'protocol': VERSION, 'fixture_hash': digest({key: value for key, value in fixtures.items() if key != 'root'}),
        'fixture_root': fixtures['root'], 'identity': identity, 'channels': list(channels),
        'policy': dict(POLICY if policy is None else policy), 'context_budget': context_budget}
    if path.exists():
        state = json.loads(path.read_text('utf-8'))
        if state['specification'] != specification:
            raise ValueError('PREPARE_PLAN_CHANGED')
        pending = [key for key, value in state['operations'].items() if value['status'] != 'COMPLETE']
        if pending:
            raise RuntimeError('PREPARE_OPERATION_REQUIRES_RECONCILIATION:' + ','.join(pending))
        if state.get('status') == 'COMPLETE':
            return load_prepared(root)
    else:
        if client.get('/knowledge-bases').raise_for_status().json():
            raise ValueError('DELIVERY_TENANT_NOT_FRESH')
        state = {'specification': specification, 'status': 'PREPARING', 'operations': {}, 'scopes': {},
                 'planned_case_ids': [case['id'] for case in fixtures['cases']]}
        _atomic_json(path, state)

    def once(key, method, url, **kwargs):
        if key in state['operations']:
            record = state['operations'][key]
            if record['status'] != 'COMPLETE':
                raise RuntimeError('PREPARE_OPERATION_REQUIRES_RECONCILIATION:' + key)
            return deepcopy(record['response'])
        require_no_pending(physical_directory)
        record = {'method': method, 'path': url, 'status': 'STARTED'}
        state['operations'][key] = record
        _atomic_json(path, state)
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            value = response.json()
        except httpx.HTTPStatusError as exc:
            record.update(status='ERROR', http_status=exc.response.status_code)
            _atomic_json(path, state)
            raise
        except Exception as exc:
            record.update(status='UNKNOWN', diagnostic=type(exc).__name__)
            _atomic_json(path, state)
            raise
        record.update(status='COMPLETE', response=value)
        _atomic_json(path, state)
        return value

    data_scopes = {}
    for split in ('dev', 'holdout'):
        scope_receipt = once(split + ':scope', 'POST', '/knowledge-bases', json={'name': 'delivery-' + split})
        scope = scope_receipt['kb_id']
        state['scopes'][split] = scope
        _atomic_json(path, state)
        receipts = {}
        for document in (doc for doc in fixtures['documents'] if doc['split'] == split):
            raw = (Path(fixtures['root']) / document['relative_path']).read_bytes()
            if sha256(raw).hexdigest() != document['original_sha256']:
                raise ValueError('FIXTURE_ORIGINAL_CHANGED:' + document['id'])
            receipts[document['id']] = once(document['id'] + ':upload', 'POST',
                f'/knowledge-bases/{scope}/sources', files={'file': (document['filename'], raw)},
                data={'policy': json.dumps(specification['policy'])})
        build = once(split + ':build', 'POST', f'/knowledge-bases/{scope}/builds', json={'channels': list(channels)})
        published = once(split + ':publish', 'POST',
                         f'/knowledge-bases/{scope}/builds/{build["build_id"]}/publish')
        data_scopes[split] = {'scope_id': scope, 'ingested': receipts, 'build': build, 'published': published}
    state['status'], state['data_scopes'] = 'DATA_READY', data_scopes
    _atomic_json(path, state)
    return _bind_prepared_http(client, fixtures, root, platform=platform, identity=identity,
        data_scopes=data_scopes, context_budget=context_budget, state=state, state_path=path)


def load_prepared(root, *, allow_legacy=False):
    root = Path(root).resolve()
    prepared = json.loads((root / 'prepared.json').read_text('utf-8'))
    if prepared.get('protocol') != VERSION and not (allow_legacy and prepared.get('protocol') == LEGACY_VERSION):
        raise ValueError('UNKNOWN_PREPARED_PROTOCOL')
    for name, checksum in prepared['file_sha256'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('PREPARED_ARTIFACT_CHANGED:' + name)
    return prepared


def rebind_http(client, prepared_directory, output, *, platform, expected_tenant_id, physical_directory):
    """Reuse completed upload/build receipts into a new authority-bound directory.

    This function issues only GET requests. It never uploads, builds, publishes,
    replays a model response, or modifies the prior prepared directory.
    """
    previous = Path(prepared_directory).resolve()
    old = load_prepared(previous, allow_legacy=True)
    old_state_path = previous / 'prepare-state.json'
    old_state = json.loads(old_state_path.read_text('utf-8'))
    if old_state.get('status') != 'COMPLETE' or any(
            operation.get('status') != 'COMPLETE' for operation in old_state['operations'].values()):
        raise ValueError('REBIND_REQUIRES_RECONCILED_COMPLETE_RECEIPTS')
    require_no_pending(physical_directory)
    identity = client.get('/identity').raise_for_status().json()
    if identity != old['identity'] or identity.get('tenant_id') != expected_tenant_id:
        raise ValueError('DELIVERY_TENANT_MISMATCH')
    fixtures = load_delivery_fixtures(old['fixture_root'])
    if old_state.get('specification', {}).get('fixture_hash') != digest(
            {key: value for key, value in fixtures.items() if key != 'root'}):
        raise ValueError('REBIND_FROZEN_FIXTURE_CHANGED')
    for split, data in old['splits'].items():
        if (old_state['operations'][split + ':scope']['response']['kb_id'] != data['scope_id'] or
                old_state['operations'][split + ':build']['response'] != data['build'] or
                old_state['operations'][split + ':publish']['response'] != data['published']):
            raise ValueError('REBIND_RECEIPT_MISMATCH')
        for doc_id, receipt in data['ingested'].items():
            if old_state['operations'][doc_id + ':upload']['response'] != receipt:
                raise ValueError('REBIND_RECEIPT_MISMATCH')
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    state = {'status': 'DATA_READY', 'operations': deepcopy(old_state['operations']),
        'specification': {**old_state.get('specification', {}), 'protocol': VERSION},
        'planned_case_ids': list(old['planned_case_ids']),
        'rebind_from': {'directory': str(previous), 'prepared_sha256': sha256((previous / 'prepared.json').read_bytes()).hexdigest(),
                        'receipts_sha256': sha256(old_state_path.read_bytes()).hexdigest()}}
    data_scopes = {split: {key: data[key] for key in ('scope_id', 'ingested', 'build', 'published')}
                   for split, data in old['splits'].items()}
    return _bind_prepared_http(client, fixtures, root, platform=platform, identity=identity, data_scopes=data_scopes,
                              context_budget=old['context_budget'], state=state, state_path=root / 'prepare-state.json')


def _fragment(binding, source, relative_start=0, text=None):
    text = binding['text'] if text is None else text
    if type(relative_start) is not int or relative_start < 0:
        raise ValueError('INVALID_PROJECTION_OFFSET')
    start = binding['parse_text_start'] + relative_start
    end = start + len(text)
    if (source.id != binding['source_id'] or source.version != binding['source_version_id'] or
            source.sha256 != binding['source_text_sha256'] or end > binding['parse_text_end'] or
            source.text[start:end] != text):
        raise ValueError('CANONICAL_CAPTURE_SPAN_MISMATCH')
    return Fragment(source_id=source.id, source_sha256=source.sha256, version=source.version,
                    kb=source.kb, start=start, end=end, text=text)


def capture_final(response, records, bindings, sources):
    """Select a logical answer call and bind only its actual transport payload."""
    models = [record for record in records if isinstance(record['payload'].get('messages'), list)]
    calls = response.get('trace', {}).get('calls', [])
    answers = [call for call in calls if call.get('role') == 'answer']
    eligible = answers or [call for call in calls if call.get('role') in ('decision', 'retrieval')]
    if not eligible:
        raise ValueError('NO_OBSERVED_FINAL_MODEL_CALL')
    call = eligible[-1]
    if (not isinstance(call.get('call_id'), str) or not call['call_id'] or
            not isinstance(call.get('request_hash'), str) or not call['request_hash']):
        raise ValueError('FINAL_LOGICAL_REQUEST_IDENTITY_UNOBSERVED')
    selected = [record for record in models if record['context'].get('call_id') == call.get('call_id') and
                record['status'] == 'COMPLETE']
    if not selected or len({record['payload_sha256'] for record in selected}) != 1:
        raise ValueError('FINAL_PHYSICAL_PAYLOAD_MISSING_OR_AMBIGUOUS')
    if any(record['context'].get('request_hash') != call['request_hash'] or
           record['context'].get('agent_role') != call['role'] for record in selected):
        raise ValueError('FINAL_PHYSICAL_REQUEST_IDENTITY_MISMATCH')
    record = selected[0]  # Repeated identical transport attempts have identical inputs.
    messages = record['payload']['messages']
    content = messages[-1].get('content')
    if not isinstance(content, str):
        raise ValueError('FINAL_PROJECTION_IS_NOT_TEXT_JSON')
    projection = json.loads(content)
    if not isinstance(projection, dict) or not isinstance(projection.get('evidence'), list):
        raise ValueError('FINAL_PROJECTION_EVIDENCE_UNOBSERVED')
    if projection.get('role') != call['role']:
        raise ValueError('FINAL_PROJECTION_ROLE_MISMATCH')
    context = canonical(projection['evidence'])
    if context not in content:
        raise ValueError('FINAL_EVIDENCE_JSON_IS_NOT_ACTUAL_PAYLOAD_SUBSTRING')
    final = []
    for item in projection['evidence']:
        if not item.get('text'):
            continue
        citation = item.get('citation_id')
        if citation not in bindings or item.get('source_id') not in sources:
            raise ValueError('UNTRACKED_FINAL_EVIDENCE')
        binding = bindings[citation]
        if (item.get('source_version_id') != binding['source_version_id'] or
                item.get('parse_snapshot_id') != binding['parse_snapshot_id'] or
                item.get('original_sha256') != binding['original_sha256'] or 'text_start' not in item):
            raise ValueError('FINAL_EVIDENCE_IDENTITY_MISMATCH')
        final.append(_fragment(binding, sources[item['source_id']], item['text_start'], item['text']))
    return tuple(final), context, tuple(messages), {'call_id': call['call_id'], 'role': call['role'],
        'physical_request_ids': [item['physical_request_id'] for item in selected],
        'payload_sha256': record['payload_sha256'], 'projection_role': projection.get('role')}


def capture_stages(response, bindings, sources):
    """Keep only stages actually observed; no copied/fabricated rerank stage."""
    stages, boundaries = {}, []

    def append(stage, citation, *, start=None, end=None):
        if citation not in bindings:
            raise ValueError('UNTRACKED_STAGE_CITATION')
        binding = bindings[citation]
        if binding['source_id'] not in sources:
            raise ValueError('UNAUTHORIZED_STAGE_SOURCE')
        fragment = _fragment(binding, sources[binding['source_id']])
        if start is not None and (start != fragment.start or end != fragment.end):
            raise ValueError('STAGE_OFFSET_DOES_NOT_MATCH_CANONICAL_BINDING')
        values = stages.setdefault(stage, [])
        if fragment not in values:
            values.append(fragment)

    for item in response.get('trace', {}).get('retrieval_stages', []):
        if item.get('candidate_original_spans') is not None:
            for span in item['candidate_original_spans']:
                append('retrieval', span['citation_id'], start=span['parse_text_start'], end=span['parse_text_end'])
            boundaries.append('pipeline_candidate_pool')
        if item.get('reranker_manifest') is not None and item.get('config', {}).get('rerank'):
            for ranked in item.get('reranked_ranking', []):
                append('rerank', ranked['citation_id'])
        for expansion in item.get('expansion', []):
            for span in expansion.get('original_spans', []):
                if span['citation_id'] not in expansion.get('excluded_parent_citation_ids', []):
                    append('parent_delivery', span['citation_id'], start=span['parse_text_start'], end=span['parse_text_end'])
    for tool in response.get('trace', {}).get('tool_calls', []):
        if tool.get('status') == 'ok' and tool.get('name') in ('search', 'search_pages', 'search_tables', 'search_semantic_pages'):
            for citation in (tool.get('result') or {}).get('evidence_ids', []):
                append('retrieval', citation)
            boundaries.append('selected_original_search_tool_delivery')
    return {key: tuple(value) for key, value in stages.items()}, sorted(set(boundaries))


class DeliveryHTTPAdapter:
    implementation = VERSION

    def __init__(self, client, *, config_name, bindings, physical_directory, artifacts, scenario_inputs=None,
                 verify_idempotent_replay=False):
        if config_name not in ('evidence_recoverable', 'evidence_delivery', 'evidence_release_candidate'):
            raise ValueError('Freeze an explicit baseline or delivery configuration')
        self.client, self.config_name, self.bindings = client, config_name, deepcopy(bindings)
        self.physical_directory, self.artifacts = Path(physical_directory), Path(artifacts)
        self.scenarios = deepcopy(scenario_inputs or {})
        if type(verify_idempotent_replay) is not bool:
            raise ValueError('INVALID_IDEMPOTENT_REPLAY_CHECK')
        self.verify_idempotent_replay = verify_idempotent_replay
        if any(not isinstance(value, dict) or set(value) - {'workflow_state', 'constraints'}
               for value in self.scenarios.values()):
            raise ValueError('SCENARIO_INPUTS_MUST_ONLY_CONTAIN_EXPLICIT_PRODUCT_INPUTS')
        self.artifacts.mkdir(parents=True, exist_ok=False)

    def run(self, request, sources, corpus):
        started = time.perf_counter()
        case_dir = self.artifacts / digest(request.case_id)
        case_dir.mkdir(exist_ok=False)
        model_records, response, session_id = [], {}, None
        turn_id = 'delivery-' + uuid4().hex
        turn_ids = []
        authority_observations = []
        base = {'case_id': request.case_id, 'corpus_hash': corpus, 'implementation': self.implementation,
                'principal': request.principal, 'acl_revision': request.acl_revision}
        try:
            try:
                require_no_pending(self.physical_directory)
            except RuntimeError as exc:
                return Trace(**base, status='NOT_RUN', error=str(exc), elapsed_ms=(time.perf_counter() - started) * 1000)
            identity = self.client.get('/identity').raise_for_status().json()
            if identity.get('principal_id') != request.principal:
                raise ValueError('HTTP_ADAPTER_PRINCIPAL_MISMATCH')
            source_map = {source.id: source for source in sources}
            if set(source_map) != set(request.allowed_sources):
                raise ValueError('ADAPTER_AUTHORIZED_SOURCE_SET_MISMATCH')
            manifests = {binding.get('build_id') for binding in self.bindings.values()
                         if binding.get('source_id') in source_map}
            if (len(manifests) != 1 or None in manifests or not request.acl_revision.isdecimal() or
                    str(int(request.acl_revision)) != request.acl_revision):
                raise ValueError('FROZEN_AUTHORITY_METADATA_UNAVAILABLE')
            expected_authority = {'scope_id': request.kb, 'manifest_id': next(iter(manifests)),
                                  'authority_revision': int(request.acl_revision)}
            try:
                observed = read_authority(self.client, request.kb)
                authority_observations.append({'phase': 'before_session', 'observed': observed})
                _assert_authority(observed, expected_authority)
            except Exception as exc:
                write_json(case_dir / 'authority-observations.json', {'expected': expected_authority,
                    'observations': authority_observations, 'diagnostic': str(exc)})
                _atomic_json(case_dir / 'state.json', {'status': 'NOT_RUN', 'diagnostic': str(exc)})
                return Trace(**base, status='NOT_RUN', error=str(exc), elapsed_ms=(time.perf_counter() - started) * 1000)
            session = self.client.post(f'/scopes/{request.kb}/sessions',
                                      json={'config_name': self.config_name}).raise_for_status().json()
            session_id = session['session_id']
            revision, conversation = session['revision'], []
            for historical in request.history:
                if (not isinstance(historical, dict) or set(historical) - {
                        'role', 'message', 'content', 'source_policy', 'constraints', 'workflow_state'} or
                        historical.get('role', 'user') != 'user' or
                        ('message' in historical and 'content' in historical)):
                    raise ValueError('HISTORY_REQUIRES_ACTUAL_EXPLICIT_USER_TURNS')
                message = historical.get('message', historical.get('content'))
                if not isinstance(message, str) or not message:
                    raise ValueError('HISTORICAL_USER_MESSAGE_REQUIRED')
                conversation.append({'message': message, **{key: value for key, value in historical.items()
                    if key in ('source_policy', 'constraints', 'workflow_state')}})
            conversation.append({'message': request.question, **self.scenarios.get(request.case_id, {})})
            for number, product_turn in enumerate(conversation):
                require_no_pending(self.physical_directory)
                observed = read_authority(self.client, request.kb)
                authority_observations.append({'phase': 'before_turn', 'turn_number': number, 'observed': observed})
                _assert_authority(observed, expected_authority)
                current_id = turn_id if number == len(conversation) - 1 else 'history-' + uuid4().hex
                turn_ids.append(current_id)
                product_input = {**product_turn, 'request_id': current_id, 'expected_revision': revision}
                write_json(case_dir / f'turn-{number:02}-request.json', {'session_id': session_id,
                    'request': product_input, 'config_name': self.config_name})
                _atomic_json(case_dir / 'state.json', {'status': 'STARTED', 'session_id': session_id,
                                                     'turn_id': current_id, 'completed_prior_turns': number})
                wire = self.client.post(f'/agent/sessions/{session_id}/turns', json=product_input)
                try:
                    response = wire.json()
                except ValueError:
                    response = {'http_status': wire.status_code}
                write_json(case_dir / f'turn-{number:02}-response.json', response)
                wire.raise_for_status()
                observed = response.get('trace', {}).get('authority')
                authority_observations.append({'phase': 'actual_turn_response', 'turn_number': number, 'observed': observed})
                _assert_authority(observed, expected_authority)
                if number < len(conversation) - 1 and response.get('status') == 'error':
                    raise ValueError('PRIOR_ACTUAL_HTTP_TURN_FAILED:' + str(response.get('stop_reason')))
                revision = response['revision']
            write_json(case_dir / 'response.json', response)
            if self.verify_idempotent_replay:
                before = {record['physical_request_id'] for record in physical_records(self.physical_directory)
                          if record['context'].get('session_id') == session_id}
                replayed = self.client.post(f'/agent/sessions/{session_id}/turns', json=product_input)
                replayed.raise_for_status()
                replay_response = replayed.json()
                after = {record['physical_request_id'] for record in physical_records(self.physical_directory)
                         if record['context'].get('session_id') == session_id}
                write_json(case_dir / 'idempotent-replay.json', {'request': product_input,
                    'response': replay_response, 'physical_requests_before': sorted(before),
                    'physical_requests_after': sorted(after), 'exact_response_equal': replay_response == response})
                if replay_response != response or after != before:
                    raise ValueError('HTTP_IDEMPOTENCY_CONTRACT_VIOLATION')
            records = [record for record in physical_records(self.physical_directory)
                       if record['context'].get('session_id') == session_id and record['context'].get('turn_id') in turn_ids]
            model_records = [record for record in records if isinstance(record['payload'].get('messages'), list)]
            final_records = [record for record in records if record['context'].get('turn_id') == turn_id]
            final, context, messages, capture = capture_final(response, final_records, self.bindings, source_map)
            stages, boundaries = capture_stages(response, self.bindings, source_map)
            stages['assembly'] = final  # Observed post-assembly payload, not a prepared callback.
            status = response.get('status')
            behavior = {'answered': 'answer', 'abstained': 'abstain', 'clarification': 'clarify', 'denied': 'deny'}.get(status)
            trace = Trace(**base, status='ERROR' if status == 'error' else 'OK',
                error=response.get('stop_reason') if status == 'error' else None, stages=stages, final=final,
                final_context=context, request_messages=messages, capture='pre_send', behavior=behavior,
                elapsed_ms=(time.perf_counter() - started) * 1000, **_usage(model_records))
            write_json(case_dir / 'capture-binding.json', {'capture': capture, 'stage_boundaries': boundaries,
                'frozen_authority': expected_authority, 'authority_observations': authority_observations,
                'physical_requests': _physical_summary(records),
                'actual_http_user_turns': len(conversation), 'turn_ids': turn_ids,
                'note': 'Different candidate observation boundaries must not be compared as identical Recall@K.'})
            write_json(case_dir / 'answer-citations.json', {key: response.get(key) for key in
                ('answer', 'claims', 'evidence', 'user_statements', 'status', 'completion', 'unresolved')})
            _atomic_json(case_dir / 'state.json', {'status': 'COMPLETE', 'session_id': session_id, 'turn_id': turn_id})
            return trace
        except Exception as exc:
            if session_id is not None:
                try:
                    records = [record for record in physical_records(self.physical_directory)
                        if record['context'].get('session_id') == session_id and record['context'].get('turn_id') in turn_ids]
                    model_records = [record for record in records if isinstance(record['payload'].get('messages'), list)]
                    write_json(case_dir / 'physical-summary.json', _physical_summary(records))
                except Exception:
                    pass
            _atomic_json(case_dir / 'state.json', {'status': 'ERROR', 'session_id': session_id,
                'turn_id': turn_id, 'diagnostic': str(exc), 'exception': type(exc).__name__})
            write_json(case_dir / 'authority-observations.json', {'observations': authority_observations,
                                                                 'diagnostic': str(exc)})
            return Trace(**base, status='ERROR', error=type(exc).__name__ + ':' + str(exc),
                         elapsed_ms=(time.perf_counter() - started) * 1000, **_usage(model_records))


def _usage(records):
    def total(key):
        values = [record.get('usage', {}).get(key) for record in records]
        return sum(values) if all(type(value) is int and value >= 0 for value in values) else None
    return {'model_calls': len(records), 'input_tokens': total('input_tokens'), 'output_tokens': total('output_tokens')}


def _physical_summary(records):
    models = [record for record in records if isinstance(record['payload'].get('messages'), list)]
    return {'physical_request_ids': [record['physical_request_id'] for record in records], **_usage(models),
        'embedding_requests': len(records) - len(models),
        'pending_or_unknown': [record['physical_request_id'] for record in records if record['status'] not in ('COMPLETE', 'ERROR')],
        'unknown_usage_model_requests': [record['physical_request_id'] for record in models
            if any(type(record.get('usage', {}).get(key)) is not int for key in ('input_tokens', 'output_tokens'))]}


def execute_prepared(client, prepared_directory, output, *, config_name, physical_directory,
                     split=None, required_stages=(), plan=None, case_ids=None):
    """Execute exactly the frozen Suite through runner.run; no alternate scoring."""
    root = Path(prepared_directory).resolve()
    prepared = load_prepared(root)
    splits = [split] if split in ('dev', 'holdout') else ['dev', 'holdout'] if split is None else None
    if splits is None:
        raise ValueError('INVALID_DELIVERY_SPLIT')
    suites, bindings = [], {}
    for name in splits:
        files = prepared['splits'][name]['files']
        suites.append(Suite.model_validate_json((root / files['suite']).read_text('utf-8')))
        bindings.update(json.loads((root / files['bindings']).read_text('utf-8')))
    available_cases = tuple(case for item in suites for case in item.cases)
    if case_ids is not None and (not isinstance(case_ids, (list, tuple)) or not case_ids or
            len(case_ids) != len(set(case_ids)) or not set(case_ids) <= {case.input.case_id for case in available_cases}):
        raise ValueError('INVALID_FROZEN_ENGINEERING_CASE_SELECTION')
    suite = Suite(id='delivery-original-files-' + (split or 'all') + ('-selected-engineering-regression' if case_ids else ''),
        label_provenance='synthetic_engineering',
        sources=tuple(source for item in suites for source in item.sources),
        evidence=tuple(evidence for item in suites for evidence in item.evidence),
        cases=tuple(case for case in available_cases if case_ids is None or case.input.case_id in case_ids))
    expected = list(case_ids) if case_ids else [case for case in prepared['planned_case_ids'] if split is None or case.startswith(split + '-')]
    if {case.input.case_id for case in suite.cases} != set(expected):
        raise ValueError('FIXED_PLANNED_CASE_DENOMINATOR_CHANGED')
    plan = plan or Plan(suite_hash=digest(suite), mode='live', required_stages=tuple(required_stages),
                        require_answer_behavior=True)
    if plan.suite_hash != digest(suite) or plan.mode != 'live':
        raise ValueError('DELIVERY_PLAN_MISMATCH')
    if not plan.require_answer_behavior:
        raise ValueError('DELIVERY_PLAN_MUST_REQUIRE_ANSWER_BEHAVIOR')
    scenarios = json.loads((root / prepared['scenario_inputs']).read_text('utf-8'))
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    artifacts = output.with_name(output.name + '-artifacts')
    adapter = DeliveryHTTPAdapter(client, config_name=config_name, bindings=bindings,
        physical_directory=physical_directory, artifacts=artifacts, scenario_inputs=scenarios)
    write_json(artifacts / 'frozen-case-selection.json', {'case_ids': expected, 'total': len(expected),
        'label_provenance': 'synthetic_engineering',
        'cohort': 'selected_previously_seen_engineering_regression' if case_ids else 'all_preregistered_cases'})
    return run(suite, plan, output, adapter=adapter)

from copy import deepcopy
from hashlib import sha256
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from . import delivery_http, runner
from .contracts import AdapterInput, Case, Source, Suite, digest, text_hash
from .delivery_fixtures import VERSION as FIXTURE_VERSION, load_delivery_fixtures
from .formal_state_machines import registry_violations, source_digest
from .acceptance_output_rules import DELIVERY, RUNNER, acceptance_output_violations


class AcceptanceOutputContracts(unittest.TestCase):
    def fixture(self, root):
        fixtures, prepared, captures = root / 'fixtures', root / 'prepared', root / 'captures'
        for path in (fixtures, prepared, captures):
            path.mkdir()
        (fixtures / 'scenario-inputs.json').write_text('{}', 'utf-8')
        identity = {'tenant_id': 'tenant', 'principal_id': 'owner'}
        suites, bound, scopes, operations = {}, {}, {}, {}
        for split in ('dev', 'holdout'):
            source = Source(id=split + '-source', kb=split, version='v1', text='Synthetic source',
                            sha256=text_hash('Synthetic source'))
            request = AdapterInput(case_id=split + '-case', kb=split, principal='owner', acl_revision='1',
                question='Synthetic final question', allowed_sources=(source.id,), as_of='2026-11-03',
                history=({'role': 'user', 'message': 'Synthetic prior question'},), context_budget=1000)
            suite = Suite(id=split, label_provenance='synthetic_engineering', sources=(source,), evidence=(),
                cases=(Case(input=request, family=split, split=split, expected_behavior='abstain'),))
            suites[split] = suite
            bound[split] = {'suite': suite, 'bindings': {split + '-citation': {
                'source_id': source.id, 'build_id': split + '-build'}}, 'documents': {}, 'expectations': {}}
            scopes[split] = {'scope_id': split, 'published': {'manifest_id': split + '-build'},
                'build': {'build_id': split + '-build'}, 'ingested': {}}
            for kind, response in (('scope', {'kb_id': split}), ('build', scopes[split]['build']),
                                   ('publish', scopes[split]['published'])):
                operations[split + ':' + kind] = {'status': 'COMPLETE', 'response': response}
        state = {'status': 'DATA_READY', 'operations': operations,
                 'planned_case_ids': ['dev-case', 'holdout-case']}
        (fixtures / 'fixtures.json').write_text(json.dumps({'schema_version': FIXTURE_VERSION,
            'documents': [], 'cases': [{'id': case} for case in state['planned_case_ids']]}), 'utf-8')
        frozen = load_delivery_fixtures(fixtures)
        state['specification'] = {'protocol': delivery_http.VERSION,
            'fixture_hash': digest({key: value for key, value in frozen.items() if key != 'root'}),
            'fixture_root': str(fixtures), 'identity': identity, 'channels': ['bm25'],
            'policy': dict(delivery_http.POLICY), 'context_budget': 1000}
        state['scopes'] = {split: split for split in scopes}
        return fixtures, prepared, captures, identity, suites, bound, scopes, state

    def client(self, captures, identity, *, unknown_at=None, corrupt_response_at=None, authority_drift_at=None):
        calls, turns, authority_reads = [], [], []

        def respond(request):
            calls.append((request.method, request.url.path))
            path = request.url.path
            if path == '/identity':
                body = identity
            elif path.endswith('/agent-authority'):
                scope = path.split('/')[2]
                authority_reads.append(scope)
                revision = 2 if authority_drift_at == len(authority_reads) else 1
                body = {'scope_id': scope, 'manifest_id': scope + '-build', 'authority_revision': revision}
            elif path.endswith('/sessions'):
                scope = path.split('/')[2]
                body = {'session_id': scope + '-session', 'revision': 0}
            else:
                self.assertTrue(path.endswith('/turns'), path)
                product = json.loads(request.content)
                session = path.split('/')[3]
                scope = session.removesuffix('-session')
                number = len(turns) + 1
                turns.append(product)
                payload = {'messages': [{'role': 'user', 'content': '{"evidence":[],"role":"retrieval"}'}]}
                record = {'physical_request_id': 'physical-' + str(number),
                    'status': 'UNKNOWN' if number == unknown_at else 'COMPLETE',
                    'ledger_settlement': 'UNCERTAIN' if number == unknown_at else 'COMMITTED',
                    'payload': payload, 'payload_sha256': digest(payload),
                    'usage': None if number == unknown_at else {'input_tokens': 7, 'output_tokens': 2},
                    'context': {'session_id': session, 'turn_id': product['request_id'],
                                'call_id': 'call-' + str(number), 'request_hash': 'request-hash', 'agent_role': 'retrieval'}}
                (captures / (record['physical_request_id'] + '.json')).write_text(json.dumps(record), 'utf-8')
                body = {'status': 'abstained', 'revision': product['expected_revision'] + 1,
                    'trace': {'authority': {'scope_id': scope, 'manifest_id': scope + '-build', 'authority_revision': 1},
                        'calls': [{'role': 'retrieval', 'call_id': record['context']['call_id'], 'request_hash': 'request-hash'}]}}
                if number == corrupt_response_at:
                    del body['revision']
            return httpx.Response(200, json=body)

        client = httpx.Client(base_url='https://synthetic.invalid', transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        return client, calls, turns, authority_reads

    def bind(self, client, fixture, *, publication=None):
        fixtures, prepared, captures, identity, suites, bound, scopes, state = fixture
        with patch.object(delivery_http, 'bind_suite', side_effect=lambda *args, split, **kwargs: bound[split]):
            if publication is None:
                return delivery_http._bind_prepared_http(client, {'root': str(fixtures)}, prepared,
                    platform=None, identity=identity, data_scopes=scopes, context_budget=1000,
                    state=state, state_path=prepared / 'prepare-state.json')
            with patch.object(delivery_http, '_atomic_json', side_effect=publication):
                return delivery_http._bind_prepared_http(client, {'root': str(fixtures)}, prepared,
                    platform=None, identity=identity, data_scopes=scopes, context_budget=1000,
                    state=state, state_path=prepared / 'prepare-state.json')

    def test_two_split_publication_checks_authority_and_commits_descriptor_last(self):
        with TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            client, calls, turns, reads = self.client(fixture[2], fixture[3])
            events, atomic = [], delivery_http._atomic_json

            def publish(path, value):
                events.append((Path(path).name, value.get('status') if isinstance(value, dict) else None))
                return atomic(path, value)

            result = self.bind(client, fixture, publication=publish)
            self.assertEqual(reads, ['dev', 'holdout', 'dev', 'dev', 'holdout', 'holdout', 'dev', 'holdout'])
            self.assertEqual(events[-2:], [('prepared.json', None), ('prepare-state.json', 'COMPLETE')])
            self.assertEqual(len(result['file_sha256']), 9)
            self.assertEqual(delivery_http.load_prepared(fixture[1]), result)
            self.assertEqual(turns, [])
            self.assertTrue(all(method == 'GET' for method, path in calls))

    def test_each_authority_boundary_rejects_drift_without_complete_publication(self):
        for at in (2, 3, 4, 5, 6, 7, 8):
            with self.subTest(at=at), TemporaryDirectory() as directory:
                fixture = self.fixture(Path(directory))
                client, _, _, _ = self.client(fixture[2], fixture[3], authority_drift_at=at)
                with self.assertRaises(ValueError):
                    self.bind(client, fixture)
                self.assertFalse((fixture[1] / 'prepared.json').exists())
                self.assertNotEqual(fixture[-1]['status'], 'COMPLETE')

    def test_partial_split_files_are_reused_but_changed_scenario_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            client, _, _, _ = self.client(fixture[2], fixture[3])
            atomic = delivery_http._atomic_json

            def fail_descriptor(path, value):
                if Path(path).name == 'prepared.json':
                    raise OSError('synthetic descriptor interruption')
                return atomic(path, value)

            with self.assertRaisesRegex(OSError, 'descriptor interruption'):
                self.bind(client, fixture, publication=fail_descriptor)
            before = {p.name: p.read_bytes() for p in fixture[1].iterdir()}
            self.assertEqual(json.loads(before['prepare-state.json'])['status'], 'BINDING')
            (fixture[0] / 'scenario-inputs.json').write_text('{"dev-case":{"constraints":{"changed":true}}}', 'utf-8')
            with self.assertRaisesRegex(ValueError, 'BOUND_PREPARE_ARTIFACT_CHANGED:scenario-inputs.json'):
                self.bind(client, fixture)
            self.assertEqual({p.name: p.read_bytes() for p in fixture[1].iterdir()}, before)
            (fixture[0] / 'scenario-inputs.json').write_text('{}', 'utf-8')
            self.bind(client, fixture)
            self.assertEqual(json.loads((fixture[1] / 'prepare-state.json').read_text())['status'], 'COMPLETE')
            for name, raw in before.items():
                if name != 'prepare-state.json':
                    self.assertEqual((fixture[1] / name).read_bytes(), raw)

    def test_existing_split_artifact_mismatch_is_rejected_without_overwrite(self):
        with TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            client, _, _, _ = self.client(fixture[2], fixture[3])
            target = fixture[1] / 'holdout-bindings.json'
            target.write_text('{"different":true}', 'utf-8')
            with self.assertRaisesRegex(ValueError, 'BOUND_PREPARE_ARTIFACT_CHANGED:holdout-bindings.json'):
                self.bind(client, fixture)
            self.assertEqual(target.read_text(), '{"different":true}')
            self.assertFalse((fixture[1] / 'prepared.json').exists())

    def test_two_cases_two_turns_capture_trace_report_manifest_and_no_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            client, calls, turns, _ = self.client(fixture[2], fixture[3])
            self.bind(client, fixture)
            report = delivery_http.execute_prepared(client, fixture[1], root / 'run',
                config_name='evidence_delivery', physical_directory=fixture[2])
            self.assertEqual(report['total'], 2)
            self.assertEqual(report['counts']['PASS'], 2)
            self.assertEqual([item['expected_revision'] for item in turns], [0, 1, 0, 1])
            for case in ('dev-case', 'holdout-case'):
                trace = json.loads((root / 'run/traces' / (digest(case) + '.json')).read_text())
                self.assertEqual((trace['model_calls'], trace['input_tokens'], trace['output_tokens']), (2, 14, 4))
                case_dir = root / 'run-artifacts' / digest(case)
                self.assertEqual(json.loads((case_dir / 'state.json').read_text())['status'], 'COMPLETE')
                self.assertEqual(json.loads((case_dir / 'capture-binding.json').read_text())['actual_http_user_turns'], 2)
            manifest = json.loads((root / 'run/manifest.json').read_text())
            for name, checksum in manifest.items():
                self.assertEqual(sha256((root / 'run' / name).read_bytes()).hexdigest(), checksum)
            self.assertTrue({'report.json', 'junit.xml', 'report.html', 'plan.json', 'suite.json'} <= manifest.keys())
            self.assertEqual(json.loads((root / 'run/state.json').read_text())['status'], 'COMPLETE')
            frozen = list(calls)
            with self.assertRaises(FileExistsError):
                delivery_http.execute_prepared(client, fixture[1], root / 'run',
                    config_name='evidence_delivery', physical_directory=fixture[2])
            self.assertEqual(calls, frozen)

    def test_unknown_second_turn_retains_prior_usage_and_next_case_not_run_in_fixed_denominator(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            client, _, turns, _ = self.client(fixture[2], fixture[3], unknown_at=2)
            self.bind(client, fixture)
            report = delivery_http.execute_prepared(client, fixture[1], root / 'run',
                config_name='evidence_delivery', physical_directory=fixture[2])
            self.assertEqual(report['total'], 2)
            self.assertEqual((report['counts']['ERROR'], report['counts']['NOT_RUN']), (1, 1))
            self.assertEqual(len(turns), 2)
            first = json.loads((root / 'run/traces' / (digest('dev-case') + '.json')).read_text())
            self.assertEqual(first['model_calls'], 2)
            self.assertIsNone(first['input_tokens'])
            summary = json.loads((root / 'run-artifacts' / digest('dev-case') / 'physical-summary.json').read_text())
            self.assertEqual(summary['pending_or_unknown'], ['physical-2'])
            self.assertEqual(summary['physical_request_ids'], ['physical-1', 'physical-2'])
            self.assertEqual(len(delivery_http.physical_records(fixture[2])), 2)

    def test_post_response_exception_retains_both_physical_calls_without_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            client, _, turns, _ = self.client(fixture[2], fixture[3], corrupt_response_at=2)
            self.bind(client, fixture)
            report = delivery_http.execute_prepared(client, fixture[1], root / 'run',
                config_name='evidence_delivery', physical_directory=fixture[2])
            self.assertEqual(report['total'], 2)
            first = json.loads((root / 'run/traces' / (digest('dev-case') + '.json')).read_text())
            self.assertEqual(first['status'], 'ERROR')
            self.assertEqual((first['model_calls'], first['input_tokens'], first['output_tokens']), (2, 14, 4))
            self.assertEqual(len({turn['request_id'] for turn in turns}), len(turns))

    def test_report_write_failure_never_marks_run_complete_or_overwrites_trace_cost(self):
        for failed in ('report.json', 'manifest.json'):
            with self.subTest(failed=failed), TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self.fixture(root)
                client, _, turns, _ = self.client(fixture[2], fixture[3])
                self.bind(client, fixture)
                write = runner.write_json

                def interrupted(path, value):
                    if Path(path).name == failed:
                        raise OSError('synthetic report interruption')
                    return write(path, value)

                with patch.object(runner, 'write_json', side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, 'report interruption'):
                        delivery_http.execute_prepared(client, fixture[1], root / 'run',
                            config_name='evidence_delivery', physical_directory=fixture[2])
                self.assertEqual(json.loads((root / 'run/state.json').read_text())['status'], 'RUNNING')
                self.assertEqual(len(turns), 4)
                self.assertEqual(sum(json.loads(p.read_text())['model_calls'] for p in (root / 'run/traces').iterdir()), 4)
                self.assertFalse((root / 'run/manifest.json').exists())

    def test_prepare_and_rebind_reuse_exact_receipts_without_mutating_http_or_source_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            client, calls, turns, _ = self.client(fixture[2], fixture[3])
            expected = self.bind(client, fixture)
            state_path = fixture[1] / 'prepare-state.json'
            state = json.loads(state_path.read_text())
            state['status'] = 'BINDING'
            delivery_http._atomic_json(state_path, state)
            with patch.object(delivery_http, 'bind_suite', side_effect=lambda *args, split, **kwargs: fixture[5][split]):
                self.assertEqual(delivery_http.prepare_http(client, fixture[0], fixture[1], platform=None,
                    expected_tenant_id='tenant', physical_directory=fixture[2], context_budget=1000,
                    channels=('bm25',)), expected)
                source = {p.name: p.read_bytes() for p in fixture[1].iterdir()}
                rebound = delivery_http.rebind_http(client, fixture[1], root / 'rebound', platform=None,
                    expected_tenant_id='tenant', physical_directory=fixture[2])
                self.assertEqual(rebound['reused_http_operation_receipts'], 6)
                self.assertEqual(rebound['new_upload_build_publish_requests'], 0)
                self.assertEqual({p.name: p.read_bytes() for p in fixture[1].iterdir()}, source)
            self.assertTrue(all(method == 'GET' for method, path in calls))
            self.assertEqual(turns, [])
            for status in ('STARTED', 'UNKNOWN', 'ERROR'):
                state['operations']['dev:scope']['status'] = status
                state['status'] = 'COMPLETE'
                delivery_http._atomic_json(state_path, state)
                with self.assertRaisesRegex(ValueError, 'REBIND_REQUIRES_RECONCILED_COMPLETE_RECEIPTS'):
                    delivery_http.rebind_http(client, fixture[1], root / status, platform=None,
                        expected_tenant_id='tenant', physical_directory=fixture[2])
                self.assertFalse((root / status).exists())
            state['operations']['dev:scope']['status'] = 'COMPLETE'
            state['operations']['dev:scope']['response']['kb_id'] = 'different'
            delivery_http._atomic_json(state_path, state)
            with self.assertRaisesRegex(ValueError, 'REBIND_RECEIPT_MISMATCH'):
                delivery_http.rebind_http(client, fixture[1], root / 'mismatch', platform=None,
                    expected_tenant_id='tenant', physical_directory=fixture[2])
            self.assertFalse((root / 'mismatch').exists())

    def test_mandatory_semantic_lint_rejects_publication_accounting_and_completion_mutants_after_hash_refresh(self):
        repository = Path(__file__).resolve().parents[2]
        self.assertEqual(acceptance_output_violations(repository), [])
        registry = json.loads((repository / 'formal/registry-v1.json').read_text())
        machines = [machine for machine in registry['machines'] if machine['id'] in
                    {'acceptance_binding_publication', 'acceptance_client_execution'}]
        self.assertEqual(len(machines), 2)
        mutants = [(DELIVERY, "if json.loads(path.read_text('utf-8')) != value:", 'if False:'),
                   (DELIVERY, "    _publish_bound_json(root / 'scenario-inputs.json', scenarios)",
                    "    write_json(root / 'scenario-inputs.json', scenarios)"),
                   (DELIVERY, "_assert_authority(read_authority(client, data_scopes[split]['scope_id']), expected)", 'None'),
                   (DELIVERY, '**_usage(model_records)', '**_usage([])'),
                   (DELIVERY, '                require_no_pending(self.physical_directory)', '                pass'),
                   (RUNNER, 'exist_ok=False', 'exist_ok=True'),
                   (RUNNER, 'for case in suite.cases:', 'for case in suite.cases[:1]:'),
                   (RUNNER, '    reports(output, report)', '    pass'),
                   (RUNNER, '    write_json(output / "manifest.json", files)', '    pass')]
        for path, before, after in mutants:
            with self.subTest(mutation=after), TemporaryDirectory() as directory:
                root = Path(directory)
                for owner in (DELIVERY, RUNNER):
                    target = root / owner
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(repository / owner, target)
                target = root / path
                original = target.read_text()
                self.assertIn(before, original)
                target.write_text(original.replace(before, after))
                reviewed = deepcopy(machines)
                for machine in reviewed:
                    for binding in machine['source_bindings']:
                        binding['sha256'] = source_digest(root, binding)
                    for model in machine['models'] + machine['negative_controls']:
                        for key in ('module', 'config'):
                            destination = root / model[key]
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(repository / model[key], destination)
                errors = registry_violations(root, {'schema_version': 1,
                    'discovery': {'roots': [], 'exclusions': []}, 'machines': reviewed})
                self.assertFalse(any('source review drift' in error for error in errors), errors)
                self.assertTrue(any(error.startswith('acceptance_output:') for error in errors), errors)

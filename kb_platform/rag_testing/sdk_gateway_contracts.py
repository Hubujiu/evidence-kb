from contextlib import contextmanager, ExitStack
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from .budget import BudgetLedger
from .contracts import digest
from .sdk_gateway import SDKGateway


PAYLOAD = {'model': 'synthetic-gateway', 'messages': [{'role': 'user', 'content': 'synthetic'}],
           'max_tokens': 10}
BODY = {'choices': [{'message': {'content': 'synthetic-answer'}}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}
CONTEXT = {'stage': 'index', 'document': 'fixture'}


@contextmanager
def gateway_fixture(respond=None, *, live=False, recovery=None):
    with TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        source = root / 'source.pdf'
        source.write_bytes(b'synthetic-local-source')
        dependencies = {'installed_code_hash': 'synthetic-sdk', 'pdfs': {
            'fixture': {'path': str(source), 'sha256': sha256(source.read_bytes()).hexdigest()}}}
        ledger = BudgetLedger(root / 'budget.sqlite3', dict(model=6, input_tokens=1000,
            output_tokens=1000, remote_embedding_calls=0, embedding_input_tokens=0))
        stack.callback(ledger.close)
        provider = SimpleNamespace(client=Mock(), provider_id='synthetic-gateway', model='synthetic-gateway',
            manifest={}, cache_dir=root / 'provider', base_url='https://synthetic.invalid/v1', _headers={})
        sends = []

        def handle(request):
            sends.append(json.loads(request.content))
            return respond(request) if respond else httpx.Response(200, json=BODY)

        output = root / 'run'
        output.mkdir()
        with ExitStack() as construct:
            construct.enter_context(patch('kb_platform.rag_testing.sdk_gateway.LunaClient', return_value=provider))
            construct.enter_context(patch('kb_platform.rag_testing.sdk_gateway.httpx.HTTPTransport',
                                          return_value=httpx.MockTransport(handle)))
            if not live:
                construct.enter_context(patch('kb_platform.rag_testing.sdk_gateway.ThreadingHTTPServer',
                    side_effect=lambda address, handler: SimpleNamespace(RequestHandlerClass=handler,
                        server_port=1, shutdown=Mock(), server_close=Mock(), serve_forever=Mock())))
                construct.enter_context(patch('kb_platform.rag_testing.sdk_gateway.threading.Thread'))
            gateway = object.__new__(SDKGateway)
            try:
                SDKGateway.__init__(gateway, 'unused', ledger, output, dependencies=dependencies, recovery=recovery)
            except Exception:
                if getattr(gateway, 'recovery_cache', None) is not None:
                    gateway.recovery_cache.close()
                raise
        stack.callback(gateway.close)
        stack.callback(gateway.recovery_cache.close)
        gateway.context = deepcopy(CONTEXT)
        yield SimpleNamespace(gateway=gateway, ledger=ledger, root=root, output=output,
                              sends=sends, dependencies=dependencies)


def local_request(gateway, payload=None):
    handler = object.__new__(gateway.server.RequestHandlerClass)
    raw = json.dumps(PAYLOAD if payload is None else payload).encode()
    handler.headers, handler.path = {'Content-Length': str(len(raw))}, '/v1/chat/completions'
    handler.rfile, handler.wfile = BytesIO(raw), BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.do_POST()
    return statuses[0], json.loads(handler.wfile.getvalue())


def records(fixture):
    return [json.loads(path.read_text('utf-8')) for path in (fixture.output / 'physical-requests').glob('*.json')]


@contextmanager
def main_fixture(*, live=False, constructor_failure=False, query=False):
    from . import pageindex_full_index, pageindex_full_query
    from .budget_batches import freeze_predecessor

    with TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        source = root / 'fixture.pdf'
        source.write_bytes(b'synthetic-main-source')
        installed = root / 'synthetic-sdk'
        installed.mkdir()
        (installed / '__init__.py').write_text('synthetic = True', 'utf-8')
        predecessor = BudgetLedger(root / 'old.sqlite3', pageindex_full_index.CAPS)
        stack.callback(predecessor.close)
        frozen = freeze_predecessor(predecessor.path)
        holder = {}
        provider = SimpleNamespace(client=Mock(), provider_id='synthetic-main', model='synthetic-main',
            manifest={}, cache_dir=root / 'provider', base_url='https://synthetic.invalid/v1', _headers={})
        module = ModuleType('pageindex')
        module.__file__ = str(installed / '__init__.py')

        class SyntheticSDK:
            def __init__(self, **options):
                self.backend = options['index_backend']
                if constructor_failure:
                    raise RuntimeError('SYNTHETIC_SDK_CONSTRUCTION_FAILURE')

            def submit_document(self, path):
                if live:
                    with httpx.Client(trust_env=False) as client:
                        response = client.post(self.backend['base_url'] + '/chat/completions', json=PAYLOAD)
                    response.raise_for_status()
                else:
                    status, body = local_request(holder['gateway'])
                    if status != 200:
                        raise RuntimeError(str(body))
                return {'doc_id': 'synthetic-document'}

            def get_tree(self, identity, *, node_summary):
                return {'synthetic': True, 'doc_id': identity, 'node_summary': node_summary}

        module.PageIndexClient = SyntheticSDK

        def construct(*args, **options):
            gateway = SDKGateway(*args, **options)
            holder['gateway'] = gateway
            stack.callback(gateway.ledger.ledger.close)
            stack.callback(gateway.close)
            return gateway

        stack.enter_context(patch.dict(sys.modules, {'pageindex': module}))
        workflow = pageindex_full_query if query else pageindex_full_index
        if query:
            suite = json.loads((Path(__file__).resolve().parents[2] / 'config/rag-testing/suite-v1.json')
                               .read_text('utf-8'))
            inputs = {'suite.json': suite, 'gold.json': {'cases': {}}, 'predecessors.json': [],
                      'results.json': {key: {'status': 'COMPLETE'} for key in ('huawei', 'fed')},
                      'protocol.json': {'synthetic': True}}
            stack.enter_context(patch.object(workflow, 'read', side_effect=lambda path: inputs[path.name]))
        else:
            stack.enter_context(patch.object(workflow, 'PDFS', {'fixture': source}))
            stack.enter_context(patch.object(workflow.importlib.metadata, 'distributions', return_value=[]))
        stack.enter_context(patch.object(workflow, 'SDKGateway', side_effect=construct))
        stack.enter_context(patch.object(workflow, 'freeze_predecessor', return_value=frozen))
        stack.enter_context(patch('kb_platform.rag_testing.sdk_gateway.LunaClient', return_value=provider))
        stack.enter_context(patch('kb_platform.rag_testing.sdk_gateway.httpx.HTTPTransport',
                                  return_value=httpx.MockTransport(lambda request: httpx.Response(200, json=BODY))))
        if not live:
            stack.enter_context(patch('kb_platform.rag_testing.sdk_gateway.ThreadingHTTPServer',
                side_effect=lambda address, handler: SimpleNamespace(RequestHandlerClass=handler,
                    server_port=1, shutdown=Mock(), server_close=Mock(), serve_forever=Mock())))
            stack.enter_context(patch('kb_platform.rag_testing.sdk_gateway.threading.Thread'))
        output = root / 'main'
        stack.enter_context(patch.object(sys, 'argv', ['pageindex_full_index', '--output', str(output)]))
        yield SimpleNamespace(run=workflow.main, output=output, holder=holder)


class SDKGatewayContracts(unittest.TestCase):
    def test_cache_failure_is_http_failure_without_resending_known_result(self):
        with gateway_fixture() as fixture:
            with patch.object(fixture.gateway.recovery_cache, 'put', side_effect=OSError('synthetic-cache')):
                status, body = local_request(fixture.gateway)
            self.assertEqual(status, 500)
            self.assertIn('error', body)
            self.assertEqual(len(fixture.sends), 1)
            self.assertEqual(records(fixture)[0]['status'], 'COMPLETE')
            self.assertEqual(records(fixture)[0]['ledger_settlement'], 'COMMITTED')
            self.assertGreaterEqual(local_request(fixture.gateway)[0], 400)
            self.assertEqual(len(fixture.sends), 1)

    def test_identical_payload_in_two_contexts_binds_each_new_physical_attempt(self):
        with gateway_fixture() as fixture:
            first = dict(CONTEXT)
            second = {**CONTEXT, 'stage': 'query'}
            identities = []
            for context in (first, second):
                fixture.gateway.context = context
                self.assertEqual(local_request(fixture.gateway), (200, BODY))
                cached = fixture.gateway.recovery_cache.get(fixture.gateway.stage_key(context, PAYLOAD),
                                                            authorize=lambda: True)
                identities.append(cached['physical_id'])
            self.assertEqual(len(set(identities)), 2)
            self.assertEqual(len(fixture.sends), 2)
            self.assertEqual({row['physical_request_id'] for row in records(fixture)}, set(identities))

    def test_request_observations_keep_admitted_context_when_owner_replaces_context(self):
        holder = {}
        changed = {**CONTEXT, 'stage': 'query'}

        def respond(request):
            holder['gateway'].context = changed
            return httpx.Response(200, json=BODY)

        with gateway_fixture(respond) as fixture:
            holder['gateway'] = fixture.gateway
            with patch('builtins.print') as report:
                self.assertEqual(local_request(fixture.gateway), (200, BODY))
            self.assertEqual(json.loads(report.call_args.args[0])['phase'], CONTEXT)
            self.assertEqual(records(fixture)[0]['context'], CONTEXT)

    def test_context_value_is_owned_and_exact_cache_identity_includes_policy(self):
        with gateway_fixture() as fixture:
            value = {**CONTEXT, 'policy': {'version': 1}}
            fixture.gateway.context = value
            value['policy']['version'] = 2
            copy = fixture.gateway.context
            copy['policy']['version'] = 3
            self.assertEqual(fixture.gateway.context['policy'], {'version': 1})
            self.assertNotEqual(fixture.gateway.stage_key(value, PAYLOAD),
                                fixture.gateway.stage_key(fixture.gateway.context, PAYLOAD))

    def test_http_error_blocks_same_run_and_explicit_recovery_links_new_identity(self):
        with gateway_fixture(lambda request: httpx.Response(500, json={'error': 'synthetic'})) as old:
            self.assertEqual(local_request(old.gateway), (500, {'error': 'synthetic'}))
            self.assertGreaterEqual(local_request(old.gateway)[0], 400)
            self.assertEqual(len(old.sends), 1)
            original = records(old)[0]
            self.assertEqual(original['status'], 'ERROR')
            self.assertEqual(original['ledger_settlement'], 'COMMITTED')
            with gateway_fixture(recovery=[old.output]) as new:
                self.assertEqual(local_request(new.gateway), (200, BODY))
                retried = records(new)[0]
                self.assertNotEqual(retried['physical_request_id'], original['physical_request_id'])
                self.assertEqual(retried['context']['retry_of'], original['physical_request_id'])
                self.assertEqual(new.ledger.summary()['physical_requests'], 1)
                with gateway_fixture(recovery=[old.output, new.output]) as replay:
                    self.assertEqual(local_request(replay.gateway), (200, BODY))
                    self.assertFalse(replay.sends)
                    self.assertEqual(replay.gateway.cache_hits[0]['physical_id'], retried['physical_request_id'])
            self.assertEqual(old.ledger.summary()['physical_requests'], 1)

    def test_unknown_and_unconfirmed_observed_results_never_recover_or_resend(self):
        from .physical_settlement_contracts import settlement_failure

        for committed in (False, True):
            with self.subTest(committed=committed), gateway_fixture() as old:
                with settlement_failure(old.ledger, committed=committed):
                    self.assertEqual(local_request(old.gateway)[0], 500)
                record = records(old)[0]
                self.assertEqual(record['response'], BODY)
                self.assertEqual(record['ledger_settlement'], 'UNCONFIRMED')
                self.assertGreaterEqual(local_request(old.gateway)[0], 400)
                self.assertEqual(len(old.sends), 1)
                with self.assertRaisesRegex(ValueError, 'UNRESOLVED_RECOVERY_REQUEST'):
                    with gateway_fixture(recovery=[old.output]):
                        self.fail('unconfirmed recovery must fail before opening the gateway')
        def timeout(request):
            raise httpx.ReadTimeout('synthetic-unknown')

        with gateway_fixture(timeout) as old:
            self.assertEqual(local_request(old.gateway)[0], 500)
            self.assertEqual(records(old)[0]['status'], 'UNKNOWN')
            self.assertEqual(old.ledger.summary()['pending_or_unknown'], 1)
            with self.assertRaisesRegex(ValueError, 'UNRESOLVED_RECOVERY_REQUEST'):
                with gateway_fixture(recovery=[old.output]):
                    self.fail('unknown recovery must not send')

    def test_recovery_retry_parent_is_lineage_not_filename_order(self):
        with gateway_fixture() as old:
            capture = old.output / 'physical-requests'
            for physical, parent in (('f' * 32, None), ('0' * 32, 'f' * 32)):
                context = dict(CONTEXT)
                if parent:
                    context.update(retry_of=parent, retry_reason='synthetic-explicit-recovery')
                (capture / (physical + '.json')).write_text(json.dumps(dict(
                    physical_request_id=physical, payload=PAYLOAD, payload_sha256=digest(PAYLOAD),
                    context=context, status='ERROR', http_status=500, ledger_settlement='COMMITTED',
                    response={'error': 'synthetic'})), 'utf-8')
            with gateway_fixture(recovery=[old.output]) as recovered:
                key = digest(recovered.gateway.stage_key(CONTEXT, PAYLOAD))
                self.assertEqual(recovered.gateway.retry_of[key], '0' * 32)
                self.assertEqual(local_request(recovered.gateway), (200, BODY))
                self.assertEqual(records(recovered)[0]['context']['retry_of'], '0' * 32)
            record = json.loads((capture / ('0' * 32 + '.json')).read_text('utf-8'))
            record['context'] = dict(CONTEXT)
            (capture / ('0' * 32 + '.json')).write_text(json.dumps(record), 'utf-8')
            with self.assertRaisesRegex(ValueError, 'AMBIGUOUS_RECOVERY_RETRY_LINEAGE'):
                with gateway_fixture(recovery=[old.output]):
                    self.fail('independent duplicate errors have no unique retry lineage')

    def test_reporting_failure_after_success_is_non_success_without_second_send(self):
        with gateway_fixture() as fixture:
            with patch('builtins.print', side_effect=OSError('synthetic-report')):
                self.assertEqual(local_request(fixture.gateway)[0], 500)
            self.assertTrue(fixture.gateway.blocked)
            self.assertEqual(records(fixture)[0]['response'], BODY)
            self.assertGreaterEqual(local_request(fixture.gateway)[0], 400)
            self.assertEqual(len(fixture.sends), 1)

    def test_duplicate_without_recovery_cache_and_closed_gateway_never_send(self):
        with gateway_fixture() as fixture:
            fixture.gateway.recovery_cache.close()
            fixture.gateway.recovery_cache = None
            self.assertEqual(local_request(fixture.gateway), (200, BODY))
            self.assertGreaterEqual(local_request(fixture.gateway)[0], 400)
            self.assertEqual(len(fixture.sends), 1)
        with gateway_fixture() as fixture:
            fixture.gateway.close()
            self.assertGreaterEqual(local_request(fixture.gateway)[0], 400)
            self.assertFalse(fixture.sends)
            with self.assertRaisesRegex(ValueError, 'GATEWAY_CLOSING'):
                fixture.gateway.context = CONTEXT

    def test_transport_identity_is_checked_against_the_new_call_not_trusted_alone(self):
        with gateway_fixture() as fixture:
            self.assertEqual(local_request(fixture.gateway), (200, BODY))
            original = records(fixture)[0]['physical_request_id']
            fixture.gateway.context = {**CONTEXT, 'stage': 'query'}
            post = fixture.gateway.client.post

            def substitute(*args, **options):
                response = post(*args, **options)
                response.extensions['_kb_physical_request_id'] = original
                return response

            with patch.object(fixture.gateway.client, 'post', side_effect=substitute):
                self.assertEqual(local_request(fixture.gateway)[0], 500)
            self.assertEqual(len(fixture.sends), 2)
            self.assertEqual(fixture.ledger.summary()['pending_or_unknown'], 0)
            self.assertIsNone(fixture.gateway.recovery_cache.get(
                fixture.gateway.stage_key(fixture.gateway.context, PAYLOAD), authorize=lambda: True))

    def test_cache_reuse_rechecks_current_source_authorization_without_sending(self):
        with gateway_fixture() as fixture:
            self.assertEqual(local_request(fixture.gateway), (200, BODY))
            self.assertEqual(local_request(fixture.gateway), (200, BODY))
            self.assertEqual(len(fixture.sends), 1)
            Path(fixture.dependencies['pdfs']['fixture']['path']).write_bytes(b'changed-synthetic-source')
            self.assertGreaterEqual(local_request(fixture.gateway)[0], 400)
            self.assertEqual(len(fixture.sends), 1)

    def test_non_200_completed_capture_is_not_replayed_as_a_200(self):
        with gateway_fixture(lambda request: httpx.Response(201, json=BODY)) as old:
            self.assertEqual(local_request(old.gateway)[0], 201)
            self.assertEqual(records(old)[0]['status'], 'COMPLETE')
            with self.assertRaisesRegex(ValueError, 'AMBIGUOUS_OR_UNREPLAYABLE_RECOVERY_RESULT'):
                with gateway_fixture(recovery=[old.output]):
                    self.fail('a different HTTP success must not become a cached 200')

    def test_recovery_rebuilds_from_capture_not_a_checksummed_old_cache_binding(self):
        with gateway_fixture() as old:
            self.assertEqual(local_request(old.gateway), (200, BODY))
            identity = records(old)[0]['physical_request_id']
            other = {**CONTEXT, 'stage': 'query'}
            old.gateway.recovery_cache.put(old.gateway.stage_key(other, PAYLOAD),
                                            dict(response=BODY, physical_id=identity))
            with gateway_fixture(recovery=[old.output]) as recovered:
                recovered.gateway.context = other
                self.assertEqual(local_request(recovered.gateway), (200, BODY))
                self.assertEqual(len(recovered.sends), 1)
                self.assertNotEqual(records(recovered)[0]['physical_request_id'], identity)
                self.assertEqual(records(recovered)[0]['context'], other)

    def test_actual_main_requires_new_output_before_starting_gateway(self):
        with main_fixture() as fixture:
            fixture.run()
            original = fixture.holder['gateway']
            with self.assertRaises(FileExistsError):
                fixture.run()
            self.assertIs(fixture.holder['gateway'], original)
            self.assertEqual(original.ledger.summary()['physical_requests'], 1)

    def test_actual_main_closes_gateway_when_sdk_construction_fails(self):
        with main_fixture(constructor_failure=True) as fixture:
            with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC_SDK_CONSTRUCTION_FAILURE'):
                fixture.run()
            gateway = fixture.holder['gateway']
            self.assertTrue(gateway.client.is_closed)
            self.assertTrue((fixture.output / 'budget-after.json').is_file())

    def test_actual_query_main_closes_gateway_when_sdk_construction_fails(self):
        with main_fixture(constructor_failure=True, query=True) as fixture:
            with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC_SDK_CONSTRUCTION_FAILURE'):
                fixture.run()
            self.assertTrue(fixture.holder['gateway'].client.is_closed)
            self.assertTrue((fixture.output / 'budget-after.json').is_file())

    def test_actual_main_uses_context_and_reports_after_capture_quiescence(self):
        with main_fixture() as fixture:
            fixture.run()
            gateway = fixture.holder['gateway']
            self.assertTrue(gateway.closed)
            record = json.loads(next((fixture.output / 'physical-requests').glob('*.json')).read_text('utf-8'))
            self.assertEqual(record['context'], CONTEXT)
            self.assertEqual(record['ledger_settlement'], 'COMMITTED')
            report = json.loads((fixture.output / 'budget-after.json').read_text('utf-8'))
            self.assertEqual(report['cumulative']['physical_requests'], 1)
            self.assertEqual(report['cumulative']['pending_or_unknown'], 0)


def live_suite():
    class SDKGatewayIntegration(unittest.TestCase):
        def test_actual_main_with_synthetic_sdk_and_loopback_gateway(self):
            with main_fixture(live=True) as fixture:
                fixture.run()
                self.assertTrue(fixture.holder['gateway'].closed)
                results = json.loads((fixture.output / 'results.json').read_text('utf-8'))
                self.assertEqual(results['fixture']['status'], 'COMPLETE')
                report = json.loads((fixture.output / 'budget-after.json').read_text('utf-8'))
                self.assertEqual(report['cumulative']['physical_requests'], 1)
                self.assertEqual(report['cumulative']['pending_or_unknown'], 0)

        def test_close_waits_for_inflight_capture_before_closing_client(self):
            entered, release, closed = Event(), Event(), Event()
            responses, failures = [], []

            def respond(request):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('SYNTHETIC_RELEASE_TIMEOUT')
                return httpx.Response(200, json=BODY)

            with gateway_fixture(respond, live=True) as fixture:
                def request():
                    try:
                        with httpx.Client(trust_env=False, timeout=10) as client:
                            responses.append(client.post(fixture.gateway.backend['base_url'] +
                                                         '/chat/completions', json=PAYLOAD))
                    except Exception as exc:
                        failures.append(exc)

                def close():
                    try:
                        fixture.gateway.close()
                    finally:
                        closed.set()

                caller = Thread(target=request)
                closer = Thread(target=close)
                caller.start()
                try:
                    self.assertTrue(entered.wait(5))
                    closer.start()
                    self.assertFalse(closed.wait(0.7))
                    self.assertFalse(fixture.gateway.client.is_closed)
                    self.assertEqual(fixture.ledger.summary()['pending_or_unknown'], 1)
                finally:
                    release.set()
                    caller.join(10)
                    if closer.ident is not None:
                        closer.join(10)
                self.assertFalse(caller.is_alive())
                self.assertFalse(closer.is_alive())
                self.assertFalse(failures)
                self.assertTrue(closed.is_set())
                self.assertTrue(fixture.gateway.client.is_closed)
                self.assertEqual([response.status_code for response in responses], [200])
                self.assertEqual(fixture.ledger.summary()['pending_or_unknown'], 0)
                self.assertEqual(records(fixture)[0]['ledger_settlement'], 'COMMITTED')

    return unittest.defaultTestLoader.loadTestsFromTestCase(SDKGatewayIntegration)

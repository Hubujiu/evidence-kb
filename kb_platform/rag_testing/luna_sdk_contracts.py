"""Real SDK over synthetic HTTP transport; no remote provider calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from kb_platform.agent_engine import execution_scope
from kb_platform.agent_luna import LunaModelProvider
from kb_platform.agent_model import AgentModel
from kb_platform.provider_admission import CaptureAdmissionError
from kb_platform.provider_runtime import RuntimePolicyError
from kb_platform.delivery import _serialize_client_sends
from kb_platform.remote import RemoteModelError
from .budget import BudgetLedger
from .contracts import digest
from .provider_capture import CapturedLuna, observe_client


def completion(text='{"count":1}'):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
                                   'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})


class LunaSDKContracts(unittest.TestCase):
    def test_cancellation_before_transport_is_zero_send_without_unknown_receipt(self):
        for cancel_while_queued in (False, True):
            with self.subTest(cancel_while_queued=cancel_while_queued):
                state = self.setup_provider([completion()])
                cancellation = Event()
                released = []

                class Queue:
                    def acquire(self, timeout=None):
                        cancellation.set()
                        return True

                    def release(self):
                        released.append(True)

                _serialize_client_sends(state.provider, Queue())
                with execution_scope(deadline_seconds=30, cancellation=cancellation):
                    response = state.provider._request_response

                    def cancel_after_receipt(*args):
                        if not cancel_while_queued:
                            cancellation.set()
                        return response(*args)

                    with patch.object(state.provider, '_request_response', side_effect=cancel_after_receipt):
                        with self.assertRaises(RuntimePolicyError) as caught:
                            self.call(state)
                self.assertEqual(caught.exception.code, 'PROVIDER_CANCELLED')
                self.assertEqual(state.requests, [])
                self.assertEqual(self.records(state), [])
                self.assertEqual(state.ledger.summary()['physical_requests'], 0)
                self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
                receipt, = self.receipts(state)
                self.assertEqual(receipt['status'], 'ERROR')
                self.assertEqual(list(state.provider._receipts.directory.glob('*.pending')), [])
                self.assertEqual(released, [True] if cancel_while_queued else [])
                self.call(state)
                self.assertEqual(len(state.requests), 1)
                self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)

    def test_direct_provider_cancellation_before_receipt_keeps_zero_attempts(self):
        state = self.setup_provider([completion()])
        cancellation = Event()
        trace = {'request_id': 'direct-cancelled', 'attempt': 0}
        with execution_scope(deadline_seconds=30, cancellation=cancellation):
            cancellation.set()
            with self.assertRaises(RemoteModelError) as caught:
                self.call(state, trace=trace)
        self.assertEqual(caught.exception.code, 'PROVIDER_CANCELLED')
        self.assertEqual(state.requests, [])
        self.assertEqual(self.records(state), [])
        self.assertEqual(self.receipts(state), [])
        self.assertEqual(trace['attempt'], 0)
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
        self.call(state)
        self.assertEqual(len(state.requests), 1)

    def test_direct_provider_retry_obeys_parent_cancellation(self):
        cancellation = Event()

        def cancel_with_received_response(request):
            cancellation.set()
            return httpx.Response(503, json={})

        state = self.setup_provider([cancel_with_received_response])
        trace = {'request_id': 'cancelled-retry', 'attempt': 0}
        with execution_scope(deadline_seconds=30, cancellation=cancellation):
            with self.assertRaises(RemoteModelError) as caught:
                self.call(state, trace=trace)
        self.assertEqual(caught.exception.code, 'PROVIDER_CANCELLED')
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(trace['attempt'], 1)
        self.assertEqual(state.provider._issued_requests, 1)
        receipt, = self.receipts(state)
        self.assertEqual(receipt['status'], 'ERROR')
        self.assertEqual(state.ledger.summary()['physical_requests'], 1)
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
        self.assertEqual(state.ledger.summary()['unknown_usage_requests'], 1)
        self.assertEqual(state.ledger.summary()['effective_usage_or_reserved']['output_tokens'], 30)

    def test_direct_provider_timeout_cannot_exceed_parent_deadline(self):
        for explicit_deadline in (False, True):
            with self.subTest(explicit_deadline=explicit_deadline):
                state = self.setup_provider([completion()], timeout_seconds=180)
                trace = {'request_id': uuid4().hex, 'attempt': 0}
                clock = [100.0]
                with patch('kb_platform.remote.time.monotonic', side_effect=lambda: clock[0]):
                    if explicit_deadline:
                        trace['deadline_monotonic'] = clock[0] + 60
                    with execution_scope(deadline_seconds=10):
                        clock[0] += 1
                        self.call(state, trace=trace)
                for timeout in state.requests[0].extensions['timeout'].values():
                    self.assertGreater(timeout, 0)
                    self.assertEqual(timeout, 9)

    def test_direct_provider_expired_parent_deadline_is_zero_send(self):
        state = self.setup_provider([])
        trace = {'request_id': 'expired-parent', 'attempt': 0}
        clock = [100.0]
        with patch('kb_platform.remote.time.monotonic', side_effect=lambda: clock[0]):
            with execution_scope(deadline_seconds=10):
                clock[0] = 111.0
                with self.assertRaises(RemoteModelError) as caught:
                    self.call(state, trace=trace)
        self.assertEqual(caught.exception.code, 'PROVIDER_DEADLINE_EXCEEDED')
        self.assertEqual(state.requests, [])
        self.assertEqual(self.receipts(state), [])
        self.assertEqual(trace['attempt'], 0)
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)

    def test_delivery_queue_deadline_reaches_sdk_and_physical_meter(self):
        state = self.setup_provider([completion()])
        clock = [100.0]

        class Queue:
            def acquire(self, timeout):
                clock[0] = 120.0
                return True

            def release(self):
                pass

        _serialize_client_sends(state.provider, Queue())
        with patch('kb_platform.agent_runtime.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(RuntimePolicyError) as caught:
                self.call(state, trace={'request_id': 'queued', 'attempt': 0, 'deadline_monotonic': 110.0})
        self.assertEqual(caught.exception.code, 'PROVIDER_DEADLINE_EXCEEDED')
        self.assertEqual(state.requests, [])
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
        receipt, = self.receipts(state)
        self.assertEqual(receipt['status'], 'ERROR')

    def setup_provider(self, outcomes, *, caps=None, **options):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / 'providers.json'
        config.write_text(json.dumps({'cache_dir': str(root / 'cache'), 'log_path': str(root / 'calls.jsonl'),
            'luna': {'api_key': 'synthetic-private-key', 'base_url': 'https://example.invalid/v1',
                     'model': 'synthetic-luna', 'retries': 2, 'cache_mode': 'disabled', **options}}), encoding='utf-8')
        provider = CapturedLuna(config)
        self.addCleanup(provider.close)
        ledger = BudgetLedger(root / 'budget.sqlite3', caps or {
            'model': 10, 'input_tokens': 100000, 'output_tokens': 100000,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        requests = []
        outcomes = iter(outcomes)

        def handle(request):
            requests.append(request)
            outcome = next(outcomes)
            if callable(outcome):
                outcome = outcome(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        observe_client(provider, ledger, root / 'capture', category='sdk-contract', inner=httpx.MockTransport(handle))
        return SimpleNamespace(provider=provider, ledger=ledger, requests=requests, root=root, config=config)

    def call(self, state, *, trace=None, payload=None):
        payload = payload or {'model': 'synthetic-luna', 'messages': [{'role': 'user', 'content': '中文 control'}],
                              'max_tokens': 30}
        trace = trace if trace is not None else {'request_id': uuid4().hex, 'attempt': 0}
        with patch('kb_platform.remote.time.sleep'):
            return state.provider._post('/chat/completions', payload, state.provider._headers,
                                        'sdk-contract', digest(payload), trace=trace)

    def records(self, state):
        return [json.loads(path.read_text('utf-8')) for path in (state.root / 'capture').glob('*.json')]

    def receipts(self, state):
        return sorted((json.loads(path.read_text('utf-8')) for path in
                       state.provider._receipts.directory.glob('*.json')), key=lambda row: row['attempt'])

    def test_exact_wire_payload_endpoint_and_accounting(self):
        state = self.setup_provider([completion()])
        payload = {'model': 'synthetic-luna', 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '中文'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}],
            'max_tokens': 30, 'response_format': {'type': 'json_object'}}
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'ambient-key', 'OPENAI_ORG_ID': 'ambient-org',
                'OPENAI_PROJECT_ID': 'ambient-project', 'OPENAI_CUSTOM_HEADERS': 'X-Ambient: unexpected',
                'OPENAI_BASE_URL': 'https://unexpected.invalid'}):
            self.call(state, payload=payload)
        self.assertEqual(len(state.requests), 1)
        request = state.requests[0]
        self.assertEqual(str(request.url), 'https://example.invalid/v1/chat/completions')
        self.assertEqual(request.headers['Authorization'], 'Bearer synthetic-private-key')
        for name in ('OpenAI-Organization', 'OpenAI-Project', 'X-Ambient'):
            self.assertNotIn(name, request.headers)
        self.assertEqual(json.loads(request.content), payload)
        record, = self.records(state)
        self.assertEqual(record['payload'], payload)
        self.assertEqual(record['payload_sha256'], digest(payload))
        self.assertEqual(record['context']['request_hash'], digest(payload))
        self.assertEqual(record['status'], 'COMPLETE')
        self.assertNotIn('synthetic-private-key', json.dumps(record))
        self.assertEqual(state.ledger.summary()['effective_usage_or_reserved'],
                         {'model': 1, 'input_tokens': 10, 'output_tokens': 5,
                          'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        self.assertFalse(state.provider.client.is_closed)

    def test_outer_retry_links_distinct_physical_requests_and_keeps_unknown_usage(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                state = self.setup_provider([httpx.Response(status, headers={'Retry-After': '0'}, json={}), completion()])
                self.call(state)
                self.assertEqual(len(state.requests), 2)
                first, second = self.receipts(state)
                self.assertEqual((first['status'], second['status']), ('ERROR', 'COMPLETE'))
                self.assertEqual(second['retry_of'], first['physical_request_id'])
                self.assertTrue(second['retry_reason'])
                summary = state.ledger.summary()
                self.assertEqual(summary['physical_requests'], 2)
                self.assertEqual(summary['unknown_usage_requests'], 1)
                self.assertEqual(summary['effective_usage_or_reserved']['output_tokens'], 35)

    def test_sdk_does_not_retry_permanent_errors_or_follow_redirects(self):
        for status in (400, 401, 403, 408, 409, 302, 307):
            with self.subTest(status=status):
                state = self.setup_provider([httpx.Response(status, headers={'Location': 'https://unexpected.invalid'}, json={})])
                with self.assertRaises(RemoteModelError) as caught:
                    self.call(state)
                self.assertEqual(caught.exception.code, 'PROVIDER_HTTP_ERROR')
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(state.requests), 1)
                self.assertEqual(state.ledger.summary()['physical_requests'], 1)
                if status == 403:
                    with self.assertRaises(CaptureAdmissionError):
                        self.call(state)
                    self.assertEqual(len(state.requests), 1)

    def test_unknown_transport_outcome_blocks_retry_and_restart(self):
        for error in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError):
            with self.subTest(error=error):
                state = self.setup_provider([error('synthetic interruption')])
                with self.assertRaises(RemoteModelError) as caught:
                    self.call(state)
                self.assertEqual(caught.exception.code, 'PROVIDER_OUTCOME_UNKNOWN')
                self.assertEqual(len(state.requests), 1)
                before = state.ledger.summary()
                self.assertEqual(before['pending_or_unknown'], 1)
                self.assertEqual(before['effective_usage_or_reserved']['output_tokens'], 30)
                self.assertEqual(self.receipts(state)[0]['status'], 'UNKNOWN')
                restarted = CapturedLuna(state.config)
                self.addCleanup(restarted.close)
                observe_client(restarted, state.ledger, state.root / 'capture', category='sdk-contract',
                               inner=httpx.MockTransport(lambda _: self.fail('UNKNOWN was resent')))
                state.provider = restarted
                with self.assertRaises(CaptureAdmissionError):
                    self.call(state)
                self.assertEqual(state.ledger.summary(), before)

    def test_known_connection_failure_can_retry_with_new_identity(self):
        for error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            with self.subTest(error=error):
                state = self.setup_provider([error('not sent'), completion()])
                self.call(state)
                first, second = self.receipts(state)
                self.assertEqual(second['retry_of'], first['physical_request_id'])
                self.assertEqual(second['retry_reason'], 'connection_not_sent')
                self.assertEqual(state.ledger.summary()['effective_usage_or_reserved']['output_tokens'], 5)

    def test_budget_rejection_is_zero_send_despite_sdk_exception_wrapping(self):
        state = self.setup_provider([], caps={'model': 0, 'input_tokens': 10000, 'output_tokens': 1000,
                                              'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        trace = {'request_id': 'budget-denied', 'attempt': 0}
        with self.assertRaises(CaptureAdmissionError):
            self.call(state, trace=trace)
        self.assertEqual(state.requests, [])
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)
        self.assertEqual(trace['attempt'], 0)
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(self.receipts(state)[0]['status'], 'ERROR')

    def test_retry_cannot_overrun_physical_budget(self):
        state = self.setup_provider([httpx.Response(429, json={})], caps={
            'model': 1, 'input_tokens': 10000, 'output_tokens': 1000,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        trace = {'request_id': 'bounded-retry', 'attempt': 0}
        with self.assertRaises(CaptureAdmissionError):
            self.call(state, trace=trace)
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(trace['attempt'], 1)
        self.assertEqual(state.ledger.summary()['physical_requests'], 1)

    def test_retry_count_wait_and_deadline_are_owned_by_runtime(self):
        state = self.setup_provider([httpx.Response(503, json={}), httpx.Response(503, json={})], retries=1)
        with self.assertRaises(RemoteModelError):
            self.call(state)
        self.assertEqual(len(state.requests), 2)
        for code, options, trace in (
            ('PROVIDER_RETRY_WAIT_EXCEEDED', {'max_retry_wait_seconds': 1}, None),
            ('PROVIDER_DEADLINE_EXCEEDED', {}, {'request_id': 'deadline', 'attempt': 0,
                                               'deadline_monotonic': time.monotonic() + 30}),
            ('PROVIDER_REQUEST_LIMIT', {'max_requests': 1}, None),
        ):
            with self.subTest(code=code):
                state = self.setup_provider([httpx.Response(429, headers={'Retry-After': '60' if trace else '2'}, json={})], **options)
                with self.assertRaises(RemoteModelError) as caught:
                    self.call(state, trace=trace)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(state.requests), 1)

    def test_expired_deadline_prevents_any_request(self):
        state = self.setup_provider([])
        with self.assertRaises(RemoteModelError) as caught:
            self.call(state, trace={'request_id': 'expired', 'attempt': 0, 'deadline_monotonic': time.monotonic() - 1})
        self.assertEqual(caught.exception.code, 'PROVIDER_DEADLINE_EXCEEDED')
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)

    def test_sdk_request_timeout_respects_remaining_deadline(self):
        state = self.setup_provider([completion()], timeout_seconds=180)
        self.call(state, trace={'request_id': 'short-deadline', 'attempt': 0,
                               'deadline_monotonic': time.monotonic() + 10})
        for timeout in state.requests[0].extensions['timeout'].values():
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 10)

    def test_cancelled_send_keeps_unknown_reservation(self):
        state = self.setup_provider([KeyboardInterrupt('synthetic cancellation')])
        with self.assertRaises(KeyboardInterrupt):
            self.call(state)
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(self.receipts(state)[0]['status'], 'UNKNOWN')
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 1)

    def test_received_403_with_broken_body_remains_permanent_permission_failure(self):
        class BrokenBody(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.ReadError('synthetic broken body')
                yield b''
        state = self.setup_provider([httpx.Response(403, stream=BrokenBody())])
        with self.assertRaises(RemoteModelError) as caught:
            self.call(state)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.records(state)[0]['status'], 'ERROR')
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
        with self.assertRaises(CaptureAdmissionError):
            self.call(state)
        self.assertEqual(len(state.requests), 1)

    def test_invalid_response_is_received_and_charged_without_sdk_normalization(self):
        for response, code in ((httpx.Response(200, content=b'not json'), 'PROVIDER_INVALID_RESPONSE_JSON'),
                               (httpx.Response(200, json=[]), 'PROVIDER_INVALID_RESPONSE_OBJECT')):
            with self.subTest(code=code):
                state = self.setup_provider([response])
                with self.assertRaises(RemoteModelError) as caught:
                    self.call(state)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(state.requests), 1)
                self.assertEqual(self.records(state)[0]['status'], 'COMPLETE')
                self.assertEqual(state.ledger.summary()['unknown_usage_requests'], 1)

    def test_rebinding_observer_uses_current_http_client(self):
        state = self.setup_provider([completion()])
        self.call(state)
        second_requests = []
        old = state.provider.client
        def handle(request):
            second_requests.append(request)
            return completion()
        observe_client(state.provider, state.ledger, state.root / 'capture', category='sdk-contract',
                       inner=httpx.MockTransport(handle))
        self.call(state)
        self.assertTrue(old.is_closed)
        self.assertFalse(state.provider.client.is_closed)
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(len(second_requests), 1)
        self.assertEqual(state.ledger.summary()['physical_requests'], 2)

    def test_agent_schema_business_validation_cache_and_both_ledgers(self):
        state = self.setup_provider([completion()], cache_mode='read_write')
        model = AgentModel(LunaModelProvider(state.provider))
        kwargs = dict(role='answer', schema={'type': 'object', 'properties': {'count': {'type': 'integer'}},
                      'required': ['count'], 'additionalProperties': False}, call_context={})
        messages = [{'role': 'user', 'content': 'synthetic control'}]
        self.assertEqual(model.complete(messages, **kwargs).value, {'count': 1})
        self.assertEqual(model.complete(messages, **kwargs).cache_kind, 'application_response')
        def reject(value):
            raise ValueError('EVIDENCE_DENIED')
        with self.assertRaises(RemoteModelError) as caught:
            model.complete(messages, **kwargs, validator=reject)
        self.assertEqual(caught.exception.code, 'AGENT_CACHE_CONTRACT_INVALID')
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(model.ledger.snapshot()['calls'], 1)
        self.assertEqual(model.ledger.snapshot()['tokens'], 15)
        self.assertEqual(state.ledger.summary()['physical_requests'], 1)

    def test_format_correction_is_one_new_linked_request_with_exact_schema(self):
        state = self.setup_provider([completion('invalid'), completion('{"summary":"original"}')])
        result = state.provider.complete_json('Summarize', 'original', purpose='raptor_summarize')
        self.assertEqual(result, {'summary': 'original'})
        self.assertEqual(len(state.requests), 2)
        first, second = self.receipts(state)
        self.assertEqual(second['retry_of'], first['physical_request_id'])
        self.assertEqual(second['retry_reason'], 'format_correction')
        payload = json.loads(state.requests[1].content)
        self.assertEqual(payload['response_format']['type'], 'json_schema')
        self.assertTrue(payload['response_format']['json_schema']['strict'])
        self.assertEqual(state.ledger.summary()['effective_usage_or_reserved']['output_tokens'], 10)

    def test_sdk_manifest_change_invalidates_agent_and_completion_caches(self):
        for agent in (True, False):
            with self.subTest(agent=agent):
                state = self.setup_provider([completion(), completion()], cache_mode='read_write')
                model = AgentModel(LunaModelProvider(state.provider))
                def invoke():
                    if agent:
                        return model.complete([{'role': 'user', 'content': 'control'}], role='answer',
                                              schema={'type': 'object'}, call_context={})
                    return state.provider.complete_text('system', 'control', purpose='sdk-contract')
                invoke()
                invoke()
                self.assertEqual(len(state.requests), 1)
                state.provider.manifest['transport'] = {**state.provider.manifest['transport'], 'version': 'changed'}
                invoke()
                self.assertEqual(len(state.requests), 2)

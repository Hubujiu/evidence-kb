from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import httpx

from kb_platform.database_contracts import CommitUnknown, DatabaseError
from kb_platform.llm_contract import LlmClient, LlmEndpoint, METHODS
from kb_platform.model_adapters import LunaAdapter
from kb_platform.model_errors import RemoteModelError
from kb_platform.model_service import ModelService
from kb_platform.plugin_contracts import ServiceEndpoint
from . import luna_sdk_contracts
from .delivery_http import physical_records, require_no_pending
from .physical_settlement_contracts import settlement_failure, transaction_failure


class PhysicalSettlementBoundaryContracts(unittest.TestCase):
    def service(self, responses):
        state = luna_sdk_contracts.LunaSDKContracts.setup_provider(self, responses, caps={
            'model': 2, 'input_tokens': 10000, 'output_tokens': 1000,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0}, retries=0)
        self.addCleanup(state.ledger.close)
        service = ModelService()
        self.addCleanup(service.close)
        service.register('generation', LunaAdapter(state.provider))
        endpoint = LlmEndpoint(service)
        self.addCleanup(endpoint.close)
        public = ServiceEndpoint(endpoint, METHODS)

        def exchange(request):
            return json.loads(json.dumps(public.exchange(json.loads(json.dumps(request)))))

        client = LlmClient(SimpleNamespace(catalog=public.catalog, exchange=exchange))
        return state, client.structured('generation')

    def complete(self, model):
        return model.complete([{'role': 'user', 'content': 'Return a JSON value.'}], role='answer',
            schema={'type': 'object'}, output_limit=16,
            call_context={'session_id': 'boundary-session', 'turn_id': 'boundary-turn',
                          'max_physical_calls': 1})

    def budgets(self, model):
        return (model.ledger.snapshot(), model.turn_budget_snapshot('boundary-session', 'boundary-turn'))

    def test_reserve_commit_uncertainty_is_zero_send_but_not_reservation_release(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                state, model = self.service([])
                reserve, identities = state.ledger.reserve, []

                def failed_reserve(identity, resources, **options):
                    identities.append(identity)
                    with transaction_failure(state.ledger.database, committed=committed):
                        return reserve(identity, resources, **options)

                with patch.object(state.ledger, 'reserve', side_effect=failed_reserve):
                    with self.assertRaises(RemoteModelError) as caught:
                        self.complete(model)
                self.assertIs(caught.exception.model_sent, False)
                self.assertEqual(caught.exception.call_record['attempts'], 0)
                self.assertEqual(state.requests, [])
                self.assertEqual(state.provider._issued_requests, 0)
                self.assertEqual(physical_records(state.root / 'capture'), [])
                summary = state.ledger.summary()
                self.assertEqual(summary['pending_or_unknown'], int(committed))
                self.assertEqual(summary['physical_requests'], int(committed))
                if committed:
                    entry = state.ledger.entry(identities[0])
                    self.assertEqual(entry['status'], 'STARTED')
                    self.assertEqual(entry['reserved']['model'], 1)
                    self.assertIsNone(entry['usage'])
                for budget in self.budgets(model):
                    for field in ('calls', 'tokens', 'accounted_tokens', 'unknown_usage_calls',
                                  'reserved_calls', 'reserved_tokens'):
                        self.assertEqual(budget[field], 0, (field, budget))

    def test_finish_ack_loss_preserves_observed_usage_across_public_failure(self):
        for committed in (False, True):
            for usage in ({'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
                          {}, {'prompt_tokens': 10}):
                with self.subTest(committed=committed, usage=usage):
                    body = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
                            'usage': usage}
                    state, model = self.service([httpx.Response(200, json=body)])
                    with settlement_failure(state.ledger, committed=committed) as (calls, _):
                        with self.assertRaises(RemoteModelError) as caught:
                            self.complete(model)
                    error = caught.exception
                    self.assertEqual(getattr(error, 'observed_usage', None), usage)
                    self.assertIs(getattr(error, 'model_sent', None), True)
                    self.assertIsNone(getattr(error, 'preflight_code', None))
                    self.assertEqual(error.observed_usage, usage)
                    self.assertEqual(error.call_record['attempts'], 1)
                    self.assertEqual(len(state.requests), 1)
                    self.assertEqual(state.provider._issued_requests, 1)
                    self.assertEqual(calls, ['COMPLETE'])
                    record, = physical_records(state.root / 'capture')
                    self.assertEqual(record['response'], body)
                    self.assertEqual(record['ledger_settlement'], 'UNCONFIRMED')
                    self.assertEqual(record['status'], 'COMPLETE')
                    self.assertEqual(error.physical_request_id, record['physical_request_id'])
                    entry = state.ledger.entry(record['physical_request_id'])
                    self.assertEqual(entry['status'], 'COMPLETE' if committed else 'STARTED')
                    self.assertEqual(entry['usage'], record['usage'] if committed else None)
                    with self.assertRaisesRegex(RuntimeError, 'UNRESOLVED_PHYSICAL_REQUESTS'):
                        require_no_pending(state.root / 'capture')
                    available = 'completion_tokens' in usage
                    for budget in self.budgets(model):
                        self.assertEqual(budget['calls'], 1)
                        self.assertEqual(budget['unknown_usage_calls'], 0 if available else 1)
                        self.assertEqual(budget['tokens'], 15 if available else 0)
                        self.assertEqual(budget['reserved_calls'], 0)
                        self.assertEqual(budget['reserved_tokens'], 0)
                        if available:
                            self.assertEqual(budget['accounted_tokens'], 15)
                        else:
                            self.assertGreater(budget['accounted_tokens'], 0)

    def test_capture_write_failure_keeps_original_settlement_error_cause_and_usage(self):
        state, _ = self.service([luna_sdk_contracts.completion()])
        finish, write = state.ledger.finish, Path.write_text
        failure, cause = CommitUnknown('boundary-finish'), OSError('synthetic ACK loss')
        writes, calls = [], []

        def failed_finish(*args, **options):
            calls.append(options['status'])
            finish(*args, **options)
            raise failure from cause

        def fail_capture(path, *args, **options):
            if path.parent == state.root / 'capture':
                writes.append(path)
                if len(writes) > 1:
                    raise OSError('synthetic capture publication failure')
            return write(path, *args, **options)

        trace = {'request_id': 'boundary-direct', 'attempt': 0}
        with patch.object(state.ledger, 'finish', side_effect=failed_finish):
            with patch.object(Path, 'write_text', fail_capture):
                with self.assertRaises(DatabaseError) as caught:
                    luna_sdk_contracts.LunaSDKContracts.call(self, state, trace=trace)
        self.assertIs(caught.exception, failure)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertIs(getattr(failure, 'model_sent', None), True)
        self.assertEqual(failure.observed_usage, {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})
        self.assertEqual(calls, ['COMPLETE'])
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(trace['attempt'], 1)
        self.assertEqual(failure.__notes__, ['Physical capture persistence failed: OSError'])
        record, = physical_records(state.root / 'capture')
        self.assertEqual(record['status'], 'STARTED')
        self.assertEqual(state.ledger.entry(record['physical_request_id'])['status'], 'COMPLETE')

    def test_missing_response_remains_unknown_not_zero_or_invented_usage(self):
        state, model = self.service([httpx.ReadTimeout('synthetic response loss')])
        with self.assertRaises(RemoteModelError) as caught:
            self.complete(model)
        self.assertIsNot(getattr(caught.exception, 'model_sent', None), False)
        self.assertFalse(hasattr(caught.exception, 'observed_usage'))
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(state.provider._issued_requests, 1)
        record, = physical_records(state.root / 'capture')
        self.assertEqual(record['status'], 'UNKNOWN')
        self.assertIsNone(record['usage'])
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 1)
        for budget in self.budgets(model):
            self.assertEqual(budget['calls'], 1)
            self.assertEqual(budget['tokens'], 0)
            self.assertEqual(budget['unknown_usage_calls'], 1)
            self.assertGreater(budget['accounted_tokens'], 0)

    def test_capture_only_failure_after_confirmed_finish_keeps_public_observed_usage(self):
        state, model = self.service([luna_sdk_contracts.completion()])
        write, writes = Path.write_text, []

        def fail_capture(path, *args, **options):
            if path.parent == state.root / 'capture':
                writes.append(path)
                if len(writes) > 1:
                    raise OSError('synthetic final capture failure')
            return write(path, *args, **options)

        with patch.object(state.ledger, 'finish', wraps=state.ledger.finish) as finish:
            with patch.object(Path, 'write_text', fail_capture):
                with self.assertRaises(RemoteModelError) as caught:
                    self.complete(model)
        self.assertEqual(finish.call_count, 1)
        self.assertIs(caught.exception.model_sent, True)
        self.assertEqual(caught.exception.observed_usage,
                         {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertEqual(len(state.requests), 1)
        record, = physical_records(state.root / 'capture')
        self.assertEqual(record['status'], 'STARTED')
        self.assertEqual(state.ledger.entry(record['physical_request_id'])['status'], 'COMPLETE')
        with self.assertRaisesRegex(RuntimeError, 'UNRESOLVED_PHYSICAL_REQUESTS'):
            require_no_pending(state.root / 'capture')
        for budget in self.budgets(model):
            self.assertEqual((budget['calls'], budget['tokens'], budget['unknown_usage_calls']), (1, 15, 0))

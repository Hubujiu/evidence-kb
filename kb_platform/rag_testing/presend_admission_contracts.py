from contextvars import copy_context
import json
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.agent_engine import execution_scope
from kb_platform.database_contracts import CommitUnknown
from kb_platform.model_errors import RemoteModelError, error_details, model_error
from kb_platform.provider_runtime import RuntimePolicyError
from . import luna_sdk_contracts, model_failure_boundary_contracts


class PresendAdmissionContracts(unittest.TestCase):
    def exercise_wait(self, interruption, *, publication='acknowledged'):
        state = luna_sdk_contracts.LunaSDKContracts.setup_provider(self,
            [luna_sdk_contracts.completion()], timeout_seconds=180, retries=0)
        self.addCleanup(state.ledger.close)
        receipts = state.provider._receipts
        clock, cancellation = [100.0], Event()
        entered, release = Event(), Event()
        trace = {'request_id': 'waiting-admission', 'attempt': 0}
        failures, results, finishes = [], [], []
        storage_error = CommitUnknown('receipt-' + publication)
        cause = OSError('synthetic receipt publication failure')
        original_write, original_finish = receipts._write_path, receipts.finish

        def write(target, row):
            if row['status'] == 'STARTED':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('PRESEND_ADMISSION_BARRIER_TIMEOUT')
            elif row['status'] == 'ERROR' and target.suffix == '.json':
                if publication == 'rollback':
                    raise storage_error from cause
                if publication == 'physical_ack_lost':
                    original_write(target, row)
                    raise storage_error from cause
            original_write(target, row)

        def finish(*args, **kwargs):
            finishes.append((args, kwargs))
            original_finish(*args, **kwargs)
            if publication == 'terminal_ack_lost':
                raise storage_error from cause

        def invoke():
            try:
                results.append(luna_sdk_contracts.LunaSDKContracts.call(self, state, trace=trace))
            except BaseException as error:
                failures.append(error)

        with patch('kb_platform.remote.time.monotonic', side_effect=lambda: clock[0]), patch.object(
                receipts, '_write_path', side_effect=write), patch.object(receipts, 'finish', side_effect=finish):
            with execution_scope(deadline_seconds=10, cancellation=cancellation):
                context = copy_context()
                worker = Thread(target=context.run, args=(invoke,))
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    if interruption == 'deadline':
                        clock[0] = 111.0
                    elif interruption == 'partial_wait':
                        clock[0] = 105.0
                    else:
                        cancellation.set()
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
        return SimpleNamespace(state=state, trace=trace, failures=failures, results=results,
            finishes=finishes, storage_error=storage_error, cause=cause,
            receipts=luna_sdk_contracts.LunaSDKContracts.receipts(self, state))

    def assert_zero_send(self, observed):
        self.assertEqual(observed.state.requests, [])
        self.assertEqual(observed.results, [])
        self.assertEqual(observed.state.provider._issued_requests, 0)
        self.assertEqual(observed.trace['attempt'], 0)
        self.assertNotIn('last_physical_request_id', observed.trace)
        self.assertEqual(observed.state.ledger.summary()['physical_requests'], 0)
        self.assertEqual(observed.state.ledger.summary()['pending_or_unknown'], 0)
        self.assertEqual(len(observed.finishes), 1)
        self.assertEqual(len(observed.receipts), 1)
        self.assertIs(observed.failures[0].model_sent, False)
        self.assertIs(observed.failures[0].known_zero_send, True)
        self.assertEqual(observed.failures[0].admission_receipt_id,
                         observed.receipts[0]['physical_request_id'])

    def test_deadline_and_cancellation_during_receipt_admission_are_known_unsent(self):
        for interruption, code in (('deadline', 'PROVIDER_DEADLINE_EXCEEDED'),
                                   ('cancellation', 'PROVIDER_CANCELLED')):
            with self.subTest(interruption=interruption):
                observed = self.exercise_wait(interruption)
                self.assert_zero_send(observed)
                self.assertIsInstance(observed.failures[0], RemoteModelError)
                self.assertEqual(observed.failures[0].code, code)
                self.assertEqual(observed.failures[0].preflight_code, code)
                self.assertEqual(observed.receipts[0]['status'], 'ERROR')
                self.assertIs(observed.receipts[0]['known_zero_send'], True)
                self.assertEqual(list(observed.state.provider._receipts.directory.glob('*.pending')), [])

    def test_successful_admission_recomputes_timeout_from_remaining_deadline(self):
        observed = self.exercise_wait('partial_wait')
        self.assertEqual(observed.failures, [])
        self.assertEqual(len(observed.results), 1)
        self.assertEqual(len(observed.state.requests), 1)
        self.assertEqual(observed.state.provider._issued_requests, 1)
        self.assertEqual(observed.trace['attempt'], 1)
        self.assertEqual(set(observed.state.requests[0].extensions['timeout'].values()), {5.0})
        self.assertEqual(observed.receipts[0]['status'], 'COMPLETE')
        self.assertEqual(observed.state.ledger.summary()['physical_requests'], 1)
        self.assertEqual(len(observed.finishes), 1)

    def test_failed_unsent_publication_preserves_storage_error_cause_and_pending(self):
        for interruption, code in (('deadline', 'PROVIDER_DEADLINE_EXCEEDED'),
                                   ('cancellation', 'PROVIDER_CANCELLED')):
            for publication, status in (('rollback', 'STARTED'), ('physical_ack_lost', 'ERROR')):
                with self.subTest(interruption=interruption, publication=publication):
                    observed = self.exercise_wait(interruption, publication=publication)
                    self.assert_zero_send(observed)
                    error = observed.failures[0]
                    self.assertIs(error, observed.storage_error)
                    self.assertIs(error.__cause__, observed.cause)
                    self.assertEqual(error.code, 'COMMIT_UNKNOWN')
                    self.assertEqual(error.operation_id, 'receipt-' + publication)
                    self.assertEqual(error.preflight_code, code)
                    self.assertEqual(observed.receipts[0]['status'], status)
                    receipts = observed.state.provider._receipts
                    self.assertTrue(receipts._pending('waiting-admission').exists())
                    with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
                        receipts.begin('waiting-admission', 'payload', {
                            'retry_of': error.admission_receipt_id, 'retry_reason': 'explicit'})
                    self.assertEqual(observed.state.provider._issued_requests, 0)

    def test_lost_final_ack_keeps_durable_error_but_does_not_claim_send(self):
        observed = self.exercise_wait('deadline', publication='terminal_ack_lost')
        self.assert_zero_send(observed)
        self.assertIs(observed.failures[0], observed.storage_error)
        self.assertIs(observed.failures[0].__cause__, observed.cause)
        self.assertEqual(observed.receipts[0]['status'], 'ERROR')
        receipts = observed.state.provider._receipts
        self.assertFalse(receipts._pending('waiting-admission').exists())
        with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_ALREADY_SETTLED'):
            receipts.begin('waiting-admission', 'payload', {})

    def test_wire_normalization_keeps_storage_identity_and_preflight_facts_separate(self):
        observed = self.exercise_wait('deadline', publication='physical_ack_lost')
        self.assert_zero_send(observed)
        error = observed.failures[0]
        details = json.loads(json.dumps(error_details(error)))
        restored = model_error(details)
        self.assertEqual(restored.code, 'COMMIT_UNKNOWN')
        self.assertEqual(restored.operation_id, error.operation_id)
        self.assertEqual(restored.preflight_code, 'PROVIDER_DEADLINE_EXCEEDED')
        self.assertEqual(restored.admission_receipt_id, error.admission_receipt_id)
        self.assertIs(restored.known_zero_send, True)
        self.assertIs(restored.model_sent, False)

    def test_public_completion_retains_both_storage_uncertainty_and_zero_send_budgets(self):
        state, client = model_failure_boundary_contracts.ModelFailureBoundaryContracts.service(self, model_cap=2)
        model = client.structured('generation')
        receipts = state.provider._receipts
        original_write = receipts._write_path
        clock = [100.0]
        storage_error = CommitUnknown('receipt-public-boundary')

        def interrupt(target, row):
            original_write(target, row)
            if row['status'] == 'STARTED':
                clock[0] = 111.0
            elif row['status'] == 'ERROR':
                raise storage_error from OSError('synthetic public receipt acknowledgement loss')

        with patch('kb_platform.remote.time.monotonic', side_effect=lambda: clock[0]), patch.object(
                receipts, '_write_path', side_effect=interrupt):
            with execution_scope(deadline_seconds=10):
                with self.assertRaises(Exception) as caught:
                    model.complete([{'role': 'user', 'content': 'Return a value.'}], role='answer',
                        schema={'type': 'object'}, output_limit=16,
                        call_context={'session_id': 'presend-session', 'turn_id': 'presend-turn',
                                      'max_physical_calls': 1})
        error = caught.exception
        self.assertEqual(error.code, 'COMMIT_UNKNOWN')
        self.assertEqual(error.operation_id, 'receipt-public-boundary')
        self.assertEqual(error.preflight_code, 'PROVIDER_DEADLINE_EXCEEDED')
        self.assertIs(error.model_sent, False)
        self.assertIs(error.known_zero_send, True)
        self.assertTrue(error.admission_receipt_id)
        self.assertEqual(error.call_record['attempts'], 0)
        self.assertEqual(state.requests, [])
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)
        self.assertTrue(receipts._pending(error.call_record['request_id']).exists())
        for budget in (model.ledger.snapshot(), model.turn_budget_snapshot('presend-session', 'presend-turn')):
            for field in ('calls', 'tokens', 'accounted_tokens', 'unknown_usage_calls',
                          'reserved_calls', 'reserved_tokens'):
                self.assertEqual(budget[field], 0, (field, budget))

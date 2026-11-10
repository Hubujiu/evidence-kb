from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import httpx

from kb_platform.database_contracts import CommitUnknown, DatabaseError
from kb_platform.embeddings import EmbeddingCallError, EmbeddingError
from kb_platform.persistence_local import local_database
from kb_platform.provider_runtime import RequestReceipts, RuntimePolicyError
from .budget import BudgetLedger
from .embedding_jobs_contracts import google_audit, google_preflight_fixture
from .provider_capture import MeteredTransport, capture_unresolved, request_context
from .delivery_http import capture_final, physical_records, require_no_pending, _physical_summary
from .sdk_gateway import SDKGateway


@contextmanager
def transaction_failure(database, *, committed, operation_id='synthetic-settlement'):
    transaction = database.transaction
    failure = CommitUnknown(operation_id) if committed else DatabaseError('SYNTHETIC_TRANSACTION_ROLLED_BACK')

    @contextmanager
    def failed_transaction(**options):
        with transaction(**options) as db:
            yield db
            if not committed:
                raise failure
        raise failure

    with patch.object(database, 'transaction', side_effect=failed_transaction):
        yield failure


@contextmanager
def settlement_failure(ledger, *, committed):
    finish, calls, errors = ledger.finish, [], []

    def failed_finish(*args, **options):
        calls.append(options['status'])
        with transaction_failure(ledger.database, committed=committed) as failure:
            errors.append(failure)
            return finish(*args, **options)

    with patch.object(ledger, 'finish', side_effect=failed_finish):
        yield calls, errors


class PhysicalSettlementContracts(unittest.TestCase):
    def test_metered_response_survives_commit_loss_without_second_settlement(self):
        for committed in (False, True):
            with self.subTest(committed=committed), TemporaryDirectory() as directory:
                root = Path(directory)
                caps = dict(model=3, input_tokens=1000, output_tokens=1000,
                            remote_embedding_calls=0, embedding_input_tokens=0)
                ledger = BudgetLedger(root / 'budget.sqlite3', caps)
                self.addCleanup(ledger.close)
                sends = []
                body = {'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}

                def respond(request):
                    sends.append(request)
                    return httpx.Response(200, json=body)

                transport = MeteredTransport(httpx.MockTransport(respond), ledger, root / 'capture',
                                             category='synthetic-settlement')
                with settlement_failure(ledger, committed=committed) as (calls, failures):
                    with request_context({'call_id': 'call', 'request_hash': 'hash', 'agent_role': 'answer'}):
                        with self.assertRaises(DatabaseError) as caught:
                            transport.handle_request(httpx.Request('POST', 'https://fixture.invalid',
                                json={'messages': [{'role': 'user', 'content': json.dumps(
                                    {'role': 'answer', 'evidence': []})}], 'max_tokens': 20}))
                self.assertIs(caught.exception, failures[0])
                self.assertEqual(calls, ['COMPLETE'])
                self.assertEqual(len(sends), 1)
                record = json.loads(next((root / 'capture').glob('*.json')).read_text())
                self.assertEqual(record['status'], 'COMPLETE')
                self.assertEqual(record['response'], body)
                self.assertEqual(record['http_status'], 200)
                self.assertEqual(record['usage']['input_tokens'], 10)
                self.assertEqual(record['usage']['output_tokens'], 5)
                self.assertEqual(record['ledger_settlement'], 'UNCONFIRMED')
                if committed:
                    self.assertEqual(record['ledger_operation_id'], caught.exception.operation_id)
                entry = ledger.entry(record['physical_request_id'])
                self.assertEqual(entry['status'], 'COMPLETE' if committed else 'STARTED')
                self.assertEqual(entry['usage'], record['usage'] if committed else None)
                self.assertEqual(ledger.summary()['pending_or_unknown'], 0 if committed else 1)
                with self.assertRaisesRegex(ValueError, 'already registered'):
                    ledger.reserve(record['physical_request_id'], record['reserved'])
                records = physical_records(root / 'capture')
                with self.assertRaisesRegex(RuntimeError, 'UNRESOLVED_PHYSICAL_REQUESTS'):
                    require_no_pending(root / 'capture')
                summary = _physical_summary(records)
                self.assertEqual(summary['pending_or_unknown'], [record['physical_request_id']])
                self.assertEqual(summary['unconfirmed_settlement_requests'], [record['physical_request_id']])
                self.assertEqual((summary['input_tokens'], summary['output_tokens']), (10, 5))
                with self.assertRaisesRegex(ValueError, 'FINAL_PHYSICAL_PAYLOAD_MISSING_OR_AMBIGUOUS'):
                    capture_final({'trace': {'calls': [{'role': 'answer', 'call_id': 'call',
                        'request_hash': 'hash'}]}}, records, {}, {})

    def test_google_response_survives_commit_loss_without_second_settlement(self):
        for committed in (False, True):
            with self.subTest(committed=committed), google_preflight_fixture() as provider:
                provider.remote.platform, provider.remote._headers, provider.remote._issued_requests = 'gemini_api', {}, 0
                provider.remote._validated_vector = lambda values: values

                def respond(*args):
                    provider.remote.calls += 1
                    provider.remote._issued_requests += 1
                    return {'embedding': {'values': [1., 0.]}, 'usageMetadata': {'promptTokenCount': 2}}

                with patch.object(provider.remote, '_post', side_effect=respond):
                    with settlement_failure(provider.ledger, committed=committed) as (calls, failures):
                        with self.assertRaises(DatabaseError) as caught:
                            provider.documents_observed(['synthetic'], request_id='physical',
                                audit_context=google_audit(), authorize=lambda: True)
                self.assertIs(caught.exception, failures[0])
                self.assertEqual(calls, ['COMPLETE'])
                self.assertEqual(provider.remote.calls, 1)
                record = json.loads((provider.audit_dir / 'physical.json').read_text())
                self.assertEqual(record['status'], 'COMPLETE')
                self.assertEqual(record['vectors'], [[1., 0.]])
                self.assertEqual(record['provider_usage'], {'promptTokenCount': 2})
                self.assertEqual(record['usage'], {'remote_embedding_calls': 1, 'input_tokens': 2})
                self.assertEqual(record['ledger_settlement'], 'UNCONFIRMED')
                entry = provider.ledger.entry('physical')
                self.assertEqual(entry['status'], 'COMPLETE' if committed else 'STARTED')
                self.assertEqual(entry['usage'], record['usage'] if committed else None)
                self.assertIsNone(provider.last_observation)
                with self.assertRaises(EmbeddingError if committed else EmbeddingCallError):
                    provider.documents_observed(['synthetic'], request_id='physical',
                        audit_context=google_audit(), authorize=lambda: True)
                self.assertEqual(provider.remote.calls, 1)

    def test_capture_write_failure_does_not_replace_original_unknown_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            caps = dict(model=1, input_tokens=1000, output_tokens=1000,
                        remote_embedding_calls=0, embedding_input_tokens=0)
            ledger = BudgetLedger(root / 'budget.sqlite3', caps)
            self.addCleanup(ledger.close)
            transport = MeteredTransport(httpx.MockTransport(lambda request: httpx.Response(200, json={})),
                                         ledger, root / 'capture', category='synthetic-settlement')
            write = Path.write_text
            writes = []

            def fail_final_capture(path, *args, **kwargs):
                writes.append(path)
                if len(writes) > 1:
                    raise OSError('synthetic audit failure')
                return write(path, *args, **kwargs)

            with settlement_failure(ledger, committed=True) as (calls, failures):
                with patch.object(Path, 'write_text', fail_final_capture):
                    with self.assertRaises(CommitUnknown) as caught:
                        transport.handle_request(httpx.Request('POST', 'https://fixture.invalid',
                            json={'messages': [{'role': 'user', 'content': 'synthetic'}], 'max_tokens': 20}))
            self.assertIs(caught.exception, failures[0])
            self.assertEqual(calls, ['COMPLETE'])
            self.assertEqual(caught.exception.__notes__, ['Physical capture persistence failed: OSError'])
            self.assertEqual(ledger.summary()['physical_requests'], 1)
            self.assertEqual(json.loads(next((root / 'capture').glob('*.json')).read_text())['status'], 'STARTED')

    def test_successful_capture_failure_is_not_hidden_by_callers_active_exception(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = BudgetLedger(root / 'budget.sqlite3', dict(model=1, input_tokens=1000, output_tokens=1000,
                remote_embedding_calls=0, embedding_input_tokens=0))
            self.addCleanup(ledger.close)
            transport = MeteredTransport(httpx.MockTransport(lambda request: httpx.Response(200, json={})),
                                         ledger, root / 'capture', category='synthetic-settlement')
            write, writes = Path.write_text, []

            def fail_final_capture(path, *args, **kwargs):
                writes.append(path)
                if len(writes) > 1:
                    raise OSError('synthetic audit failure')
                return write(path, *args, **kwargs)

            try:
                raise ValueError('unrelated callers exception')
            except ValueError as ambient:
                with patch.object(Path, 'write_text', fail_final_capture):
                    with self.assertRaisesRegex(OSError, 'synthetic audit failure'):
                        transport.handle_request(httpx.Request('POST', 'https://fixture.invalid',
                            json={'messages': [{'role': 'user', 'content': 'synthetic'}], 'max_tokens': 20}))
                self.assertFalse(hasattr(ambient, '__notes__'))
            self.assertEqual(ledger.summary()['pending_or_unknown'], 0)

        with google_preflight_fixture() as provider:
            provider.remote.platform, provider.remote._headers, provider.remote._issued_requests = 'gemini_api', {}, 0
            provider.remote._validated_vector = lambda values: values

            def respond(*args):
                provider.remote._issued_requests += 1
                return {'embedding': {'values': [1., 0.]}, 'usageMetadata': {'promptTokenCount': 2}}

            try:
                raise ValueError('unrelated callers exception')
            except ValueError as ambient:
                with patch.object(provider.remote, '_post', side_effect=respond):
                    with patch.object(Path, 'write_text', side_effect=OSError('synthetic audit failure')):
                        with self.assertRaisesRegex(OSError, 'synthetic audit failure'):
                            provider.documents_observed(['synthetic'], request_id='physical',
                                audit_context=google_audit(), authorize=lambda: True)
                self.assertFalse(hasattr(ambient, '__notes__'))
            self.assertEqual(provider.ledger.entry('physical')['status'], 'COMPLETE')

    def test_capture_state_compatibility_and_unknown_usage_remain_conservative(self):
        for status in ('COMPLETE', 'ERROR'):
            with self.subTest(status=status):
                self.assertFalse(capture_unresolved({'status': status}))
                self.assertFalse(capture_unresolved({'status': status, 'ledger_settlement': 'COMMITTED'}))
                for settlement in ('UNCONFIRMED', 'invalid', None):
                    self.assertTrue(capture_unresolved({'status': status, 'ledger_settlement': settlement}))
        for status in ('STARTED', 'UNKNOWN', None):
            self.assertTrue(capture_unresolved({'status': status, 'ledger_settlement': 'COMMITTED'}))
        summary = _physical_summary([{'physical_request_id': 'unknown', 'status': 'UNKNOWN',
            'ledger_settlement': 'UNCONFIRMED', 'payload': {'messages': []}, 'usage': None}])
        self.assertEqual(summary['pending_or_unknown'], ['unknown'])
        self.assertEqual(summary['unknown_usage_model_requests'], ['unknown'])
        self.assertIsNone(summary['input_tokens'])
        self.assertIsNone(summary['output_tokens'])

    def test_sdk_recovery_never_caches_unconfirmed_terminal_capture(self):
        for status in ('COMPLETE', 'ERROR'):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'old' / 'physical-requests').mkdir(parents=True)
                (root / 'output').mkdir()
                payload = {'model': 'synthetic', 'messages': []}
                from .contracts import digest

                record = {'physical_request_id': 'physical', 'status': status, 'ledger_settlement': 'UNCONFIRMED',
                    'payload': payload, 'payload_sha256': digest(payload),
                    'context': {'stage': 'query', 'document': 'fixture'}, 'response': {'synthetic': True}}
                (root / 'old' / 'physical-requests' / 'physical.json').write_text(json.dumps(record), 'utf-8')
                ledger = BudgetLedger(root / 'budget.sqlite3', {'calls': 1})
                self.addCleanup(ledger.close)
                gateway = object.__new__(SDKGateway)
                with patch('kb_platform.rag_testing.sdk_gateway.LunaClient', return_value=Mock()):
                    with patch('kb_platform.rag_testing.sdk_gateway.httpx.Client',
                               side_effect=AssertionError('unconfirmed recovery must not open HTTP transport')):
                        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_RECOVERY_REQUEST'):
                            SDKGateway.__init__(gateway, 'unused', ledger, root / 'output',
                                recovery=[root / 'old'], dependencies={'installed_code_hash': 'synthetic',
                                    'pdfs': {'fixture': {'sha256': 'synthetic'}}})
                self.addCleanup(gateway.recovery_cache.close)
                self.assertEqual(ledger.summary()['physical_requests'], 0)
                self.assertIsNone(gateway.recovery_cache.get(gateway.stage_key(record['context'], payload),
                    authorize=lambda: True, mode='replay'))

    def test_receipt_begin_commit_loss_keeps_durable_no_replay_authority(self):
        for committed in (False, True):
            with self.subTest(committed=committed), TemporaryDirectory() as directory:
                database, owned = local_database(Path(directory) / 'receipts.sqlite3', owner='receipts')
                self.addCleanup(owned.close)
                receipts = RequestReceipts(database=database)
                with transaction_failure(database, committed=committed, operation_id='receipt-begin') as failure:
                    with self.assertRaises(DatabaseError) as caught:
                        receipts.begin('logical', 'payload', {})
                self.assertIs(caught.exception, failure)
                if committed:
                    with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
                        receipts.begin('logical', 'payload', {})
                else:
                    physical = receipts.begin('logical', 'payload', {})
                    self.assertEqual(receipts._database.read(physical)['status'], 'STARTED')

    def test_receipt_finish_commit_loss_never_weakens_unknown_provider_outcome(self):
        for committed in (False, True):
            with self.subTest(committed=committed), TemporaryDirectory() as directory:
                database, owned = local_database(Path(directory) / 'receipts.sqlite3', owner='receipts')
                self.addCleanup(owned.close)
                receipts = RequestReceipts(database=database)
                physical = receipts.begin('logical', 'payload', {})
                with transaction_failure(database, committed=committed, operation_id='receipt-finish') as failure:
                    with self.assertRaises(DatabaseError) as caught:
                        receipts.finish('logical', physical, 'UNKNOWN')
                self.assertIs(caught.exception, failure)
                self.assertEqual(receipts._database.read(physical)['status'], 'UNKNOWN' if committed else 'STARTED')
                with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
                    receipts.begin('logical', 'payload', {'retry_of': physical, 'retry_reason': 'blind-retry'})

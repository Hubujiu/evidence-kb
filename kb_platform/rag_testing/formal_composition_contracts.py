from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from kb_platform.provider_runtime import ToolExecutor
from kb_platform.remote import _Transport
from .budget import BudgetLedger
from .formal_composition import composition_violations, import_boundary_violations, nested_request_violations
from .provider_capture import MeteredTransport, request_context


ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def nested_requests(replies, *, inner_retries):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = BudgetLedger(root / 'budget.sqlite3', dict(model=8, input_tokens=10000,
            output_tokens=10000, remote_embedding_calls=0, embedding_input_tokens=0))
        transport = _Transport({'cache_dir': str(root / 'cache'), 'log_path': str(root / 'calls.jsonl')},
            {'base_url': 'https://fixture.invalid', 'model': 'fixture', 'retries': inner_retries,
             'cache_mode': 'disabled', 'circuit_failure_threshold': 5}, 'composition-fixture', trust_env=False)
        transport.client.close()
        pending, sends = iter(replies), []

        def respond(request):
            sends.append(request)
            result = next(pending)
            if result == 'unknown':
                raise httpx.ReadTimeout('synthetic post-send uncertainty', request=request)
            return httpx.Response(result, json={'answer': 'fixture',
                'usage': {'prompt_tokens': 4, 'completion_tokens': 2}})

        transport.client = httpx.Client(transport=MeteredTransport(httpx.MockTransport(respond),
            ledger, root / 'physical', category='synthetic-composition'))
        executor = ToolExecutor(max_attempts=2, sleep=lambda seconds: None)
        payload = {'messages': [{'role': 'user', 'content': 'fixture question'}], 'max_tokens': 32}

        def invoke():
            with request_context({'call_id': 'tool-fixture'}):
                return transport._post('/chat/completions', payload, {}, 'answer', 'payload-fingerprint')

        def execute():
            return executor.execute('tool-fixture', 'readonly-model-fixture', {'question': 'fixture'},
                                    invoke, read_only=True)

        try:
            with patch.object(transport._circuit, 'acquire', wraps=transport._circuit.acquire) as acquire, \
                    patch.object(transport._circuit, 'finish', wraps=transport._circuit.finish) as finish, \
                    patch('kb_platform.remote.time.sleep', return_value=None):
                yield SimpleNamespace(root=root, ledger=ledger, transport=transport, sends=sends,
                                      execute=execute, acquire=acquire, finish=finish)
        finally:
            transport.close()
            ledger.close()


def physical_rows(fixture):
    return [json.loads(path.read_text('utf-8')) for path in (fixture.root / 'physical').glob('*.json')]


def receipt_rows(fixture):
    return [json.loads(path.read_text('utf-8'))
            for path in fixture.transport._receipts.directory.glob('*.json')]


class FormalCompositionContracts(unittest.TestCase):
    def test_schema2_registered_composition_edges_have_actual_enforcement_and_contracts(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        self.assertEqual(registry['schema_version'], 2)
        self.assertEqual(composition_violations(ROOT, registry), [])

    def test_broken_semantic_edges_and_unrelated_evidence_are_rejected(self):
        original = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        mutations = []
        removed = deepcopy(original)
        removed['composition_edges'].pop()
        mutations.append(('missing edge', removed))
        duplicate = deepcopy(original)
        duplicate['composition_edges'].append(deepcopy(duplicate['composition_edges'][0]))
        mutations.append(('duplicate edge', duplicate))
        for name, field, value in (
            ('unknown participant', 'participants', ['circuit_tool_retry', 'not_registered']),
            ('unrelated existing participant', 'participants', ['circuit_tool_retry', 'session_commit']),
            ('unknown invariant', 'guarantees', ['DoesNotExist']),
            ('weaker actual invariant', 'guarantees', ['TypeOK']),
            ('unrelated actual model', 'model', 'knowledge_atomic_authority_commit'),
            ('missing test', 'contracts', ['kb_platform/rag_testing/formal_composition_contracts.py:'
                                          'FormalCompositionContracts.test_does_not_exist']),
            ('unrelated real test', 'contracts', ['kb_platform/rag_testing/formal_composition_contracts.py:'
                                                'FormalCompositionContracts.test_import_internal_access_mutations_fail_without_hash_changes']),
        ):
            changed = deepcopy(original)
            changed['composition_edges'][0][field] = value
            mutations.append((name, changed))
        for name, changed in mutations:
            with self.subTest(mutation=name):
                self.assertTrue(composition_violations(ROOT, changed))

    def test_malformed_composition_metadata_is_a_diagnostic_failure(self):
        original = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        mutations = [None, {}, {'schema_version': 2, 'machines': None}]
        for field, value in (('participants', [None]), ('participants', 'tool_executor'),
                             ('model', []), ('guarantees', [None]), ('assumptions', [None]),
                             ('contracts', [None]), ('contracts', ['missing-separator']),
                             ('contracts', ['../../outside_contracts.py:Owner.test_escape']),
                             ('contracts', ['kb_platform/rag_testing/not_present_contracts.py:Owner.test_missing'])):
            changed = deepcopy(original)
            changed['composition_edges'][0][field] = value
            mutations.append(changed)
        for value in (None, [None], [{}], 'wrong'):
            changed = deepcopy(original)
            changed['composition_edges'] = value
            mutations.append(changed)
        for index, changed in enumerate(mutations):
            with self.subTest(mutation=index):
                errors = composition_violations(ROOT, changed)
                self.assertTrue(errors)
                self.assertTrue(all(isinstance(error, str) for error in errors))

    def test_inner_retry_owns_two_physical_requests_under_one_circuit_permit(self):
        with nested_requests([503, 200], inner_retries=1) as fixture:
            outcome = fixture.execute()
            self.assertEqual((outcome.status, outcome.attempts), ('COMPLETE', 1))
            self.assertEqual(len(fixture.sends), 2)
            self.assertEqual(fixture.acquire.call_count, 1)
            self.assertEqual(fixture.finish.call_count, 1)
            self.assertEqual(fixture.finish.call_args.kwargs, {'success': True})
            receipts = receipt_rows(fixture)
            self.assertCountEqual([row['status'] for row in receipts], ['ERROR', 'COMPLETE'])
            self.assertEqual(len({row['logical_request_id'] for row in receipts}), 1)
            first = next(row for row in receipts if row['status'] == 'ERROR')
            last = next(row for row in receipts if row['status'] == 'COMPLETE')
            self.assertEqual(last['retry_of'], first['physical_request_id'])
            self.assertEqual(last['retry_reason'], 'unavailable')
            self.assertEqual(len({row['physical_request_id'] for row in receipts}), 2)
            self.assertCountEqual([row['status'] for row in physical_rows(fixture)], ['ERROR', 'COMPLETE'])
            summary = fixture.ledger.summary()
            self.assertEqual(summary['physical_requests'], 2)
            self.assertEqual(summary['pending_or_unknown'], 0)
            self.assertEqual(summary['effective_usage_or_reserved']['model'], 2)
            self.assertEqual(summary['effective_usage_or_reserved']['input_tokens'], 8)

    def test_outer_readonly_retry_acquires_a_new_permit_and_preserves_both_bills(self):
        with nested_requests([503, 200], inner_retries=0) as fixture:
            outcome = fixture.execute()
            self.assertEqual((outcome.status, outcome.attempts), ('COMPLETE', 2))
            self.assertEqual(fixture.acquire.call_count, 2)
            self.assertEqual(fixture.finish.call_count, 2)
            finishes = fixture.finish.call_args_list
            self.assertIsNot(finishes[0].args[0], finishes[1].args[0])
            self.assertEqual(finishes[0].kwargs, {'failure': True})
            self.assertEqual(finishes[1].kwargs, {'success': True})
            receipts = receipt_rows(fixture)
            self.assertEqual(len({row['logical_request_id'] for row in receipts}), 2)
            self.assertTrue(all(row['retry_of'] is None for row in receipts))
            self.assertCountEqual([row['status'] for row in receipts], ['ERROR', 'COMPLETE'])
            self.assertEqual(len(fixture.sends), 2)
            self.assertEqual(fixture.ledger.summary()['physical_requests'], 2)
            self.assertEqual(fixture.ledger.summary()['effective_usage_or_reserved']['model'], 2)

    def test_unknown_keeps_reserved_cost_and_is_not_retried_at_either_layer(self):
        with nested_requests(['unknown', 200], inner_retries=1) as fixture:
            outcome = fixture.execute()
            self.assertEqual((outcome.status, outcome.attempts), ('UNKNOWN', 1))
            self.assertEqual(outcome.error_code, 'PROVIDER_OUTCOME_UNKNOWN')
            self.assertEqual(len(fixture.sends), 1)
            self.assertEqual(fixture.acquire.call_count, 1)
            self.assertEqual(fixture.finish.call_count, 1)
            self.assertEqual([row['status'] for row in receipt_rows(fixture)], ['UNKNOWN'])
            records = physical_rows(fixture)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['status'], 'UNKNOWN')
            summary = fixture.ledger.summary()
            self.assertEqual(summary['physical_requests'], 1)
            self.assertEqual(summary['pending_or_unknown'], 1)
            self.assertEqual(summary['effective_usage_or_reserved'], records[0]['reserved'])
            self.assertGreater(summary['effective_usage_or_reserved']['input_tokens'], 0)
            self.assertEqual(summary['effective_usage_or_reserved']['output_tokens'], 32)

    def test_terminal_tool_reuse_never_reenters_circuit_receipts_or_ledger(self):
        for response in (200, 'unknown'):
            with self.subTest(response=response), nested_requests([response], inner_retries=1) as fixture:
                original = fixture.execute()
                before = fixture.ledger.summary(), receipt_rows(fixture), physical_rows(fixture)
                reused = fixture.execute()
                self.assertTrue(reused.reused)
                self.assertEqual((reused.status, reused.value, reused.attempts),
                                 (original.status, original.value, original.attempts))
                self.assertEqual(fixture.acquire.call_count, 1)
                self.assertEqual(fixture.finish.call_count, 1)
                self.assertEqual(len(fixture.sends), 1)
                self.assertEqual((fixture.ledger.summary(), receipt_rows(fixture), physical_rows(fixture)), before)

    def test_reviewed_nested_owner_and_import_boundary_pass_without_hashes(self):
        self.assertEqual(nested_request_violations(ROOT), [])
        self.assertEqual(import_boundary_violations(ROOT), [])

    def test_nested_owner_semantic_mutations_fail_without_hash_changes(self):
        original = (ROOT / 'kb_platform/remote.py').read_text('utf-8')
        mutations = [
            ('@_provider_guard\n    def _post', 'def _post'),
            ('permit = self._circuit.acquire()', 'permit = None'),
            ('result = operation(self, *args, **kwargs)',
             'self._circuit.finish(permit, success=True)\n            result = operation(self, *args, **kwargs)'),
            ('except BaseException as exc:\n            failure = classify_failure(exc)',
             'except Exception as exc:\n            failure = classify_failure(exc)'),
            ('self._circuit.finish(permit, success=True)', 'self._circuit.finish(self._circuit.acquire(), success=True)'),
            ('result = operation(self, *args, **kwargs)',
             'self._receipts = None\n            result = operation(self, *args, **kwargs)'),
            ('failure=not admission and (failure.retryable', 'failure=(failure.retryable'),
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'kb_platform/remote.py'
            path.parent.mkdir()
            for before, after in mutations:
                with self.subTest(mutation=before):
                    self.assertIn(before, original)
                    path.write_text(original.replace(before, after, 1), encoding='utf-8')
                    self.assertTrue(nested_request_violations(root))
            path.write_text(original + '\ndef bypass(self):\n    self._circuit.acquire()\n', encoding='utf-8')
            self.assertTrue(nested_request_violations(root))

    def test_import_internal_access_mutations_fail_without_hash_changes(self):
        original = (ROOT / 'kb_platform/knowledge_transfer.py').read_text('utf-8')
        signature = 'def import_knowledge(target, package, *, expected_identity, target_offline=False):'
        self.assertIn(signature, original)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'kb_platform/knowledge_transfer.py'
            path.parent.mkdir()
            for attribute in ('_package', '_metadata', '_space'):
                with self.subTest(attribute=attribute):
                    path.write_text(original.replace(signature, signature + '\n    leaked = target.' + attribute, 1),
                                    encoding='utf-8')
                    self.assertTrue(import_boundary_violations(root))

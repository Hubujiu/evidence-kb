import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.agent_model import CompletionEnvelope
from kb_platform.knowledge_contracts import MODEL_METHODS, ModelEndpoint
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_host import PluginHost, PluginSpec


class ModelReceiptContracts(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.calls = []
        self.model = SimpleNamespace(complete=self.complete, accept_completion=Mock())
        self.endpoint = ModelEndpoint(self.model)
        self.addCleanup(self.endpoint.close)
        self.wire = ServiceEndpoint(self.endpoint, MODEL_METHODS)
        clock = patch('kb_platform.knowledge_contracts.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.request = {'messages': [{'role': 'user', 'content': 'Synthetic receipt fixture'}],
                        'role': 'answer', 'schema': {'type': 'object'}, 'call_context': {}, 'output_limit': 10}

    def complete(self, **request):
        self.calls.append(request)
        return CompletionEnvelope({'count': 1}, 'synthetic-request',
                                  {'available': True, 'total_tokens': 15}, 1, 'stop', 'miss', 'synthetic-hash')

    def test_inflight_receipt_reservation_survives_ttl_and_bounds_public_capacity(self):
        entered, release = Event(), Event()

        def delayed(**request):
            if request['call_context'].get('old'):
                entered.set()
                if not release.wait(15):
                    raise RuntimeError('SYNTHETIC_FIXTURE_TIMEOUT')
            return self.complete(**request)

        self.model.complete = delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.wire.complete, {**self.request, 'call_context': {'old': True}})
            try:
                self.assertTrue(entered.wait(15))
                self.now = 901
                for _ in range(63):
                    self.wire.complete(self.request)
                with self.assertRaises(ContractError) as caught:
                    self.wire.complete(self.request)
                self.assertEqual(caught.exception.code, 'MODEL_VALIDATION_CAPACITY')
                self.assertEqual(len(self.calls), 63)
            finally:
                release.set()
            response = old.result(timeout=15)
        self.assertEqual(len(self.calls), 64)
        self.assertEqual(len(self.endpoint._pending), 64)
        self.assertEqual(response['completion']['usage'], {'available': True, 'total_tokens': 15})
        self.assertEqual(response['completion']['attempts'], 1)
        self.assertEqual(self.wire.settle({'token': response['token'], 'accept': True}), {})
        self.model.accept_completion.assert_called_once()

    def test_only_expired_ready_receipts_release_capacity_and_completion_error_releases_slot(self):
        for _ in range(64):
            self.wire.complete(self.request)
        self.now = 900
        with self.assertRaises(ContractError) as caught:
            self.wire.complete(self.request)
        self.assertEqual(caught.exception.code, 'MODEL_VALIDATION_CAPACITY')
        self.now = 901
        response = self.wire.complete(self.request)
        self.assertEqual(len(self.endpoint._pending), 1)
        self.wire.settle({'token': response['token'], 'accept': False})
        self.model.accept_completion.assert_not_called()
        self.model.complete = Mock(side_effect=ValueError('SYNTHETIC_COMPLETION_FAILURE'))
        with self.assertRaises(ContractError):
            self.wire.complete(self.request)
        self.assertEqual(self.endpoint._pending, {})

    def test_accept_reject_expiry_and_cache_failure_are_one_shot_and_preserve_known_receipt(self):
        for outcome in ('accept', 'reject', 'expiry', 'cache-failure', 'close'):
            with self.subTest(outcome=outcome):
                self.model.accept_completion.reset_mock(side_effect=True)
                response = self.wire.complete(self.request)
                request = {'token': response['token'], 'accept': outcome != 'reject'}
                if outcome == 'expiry':
                    self.now += 901
                if outcome == 'close':
                    self.endpoint.close()
                if outcome == 'cache-failure':
                    self.model.accept_completion.side_effect = ValueError('SYNTHETIC_CACHE_FAILURE')
                    with self.assertRaises(ContractError) as caught:
                        self.wire.settle(request)
                    self.assertEqual(caught.exception.code, 'MODEL_CACHE_COMMIT_FAILED')
                    self.assertEqual(caught.exception.details['call_record']['usage']['total_tokens'], 15)
                elif outcome in ('expiry', 'close'):
                    with self.assertRaises(ContractError) as caught:
                        self.wire.settle(request)
                    self.assertEqual(caught.exception.code, 'MODEL_VALIDATION_NOT_FOUND')
                else:
                    self.assertEqual(self.wire.settle(request), {})
                self.assertEqual(self.model.accept_completion.call_count, int(outcome in ('accept', 'cache-failure')))
                with self.assertRaises(ContractError) as caught:
                    self.wire.settle(request)
                self.assertEqual(caught.exception.code, 'MODEL_VALIDATION_NOT_FOUND')


class ModelReceiptLintContracts(unittest.TestCase):
    def test_actual_guard_and_equivalent_conjunction_are_accepted_but_mutants_rejected(self):
        from scripts.check_project import model_receipt_violations

        path = Path(__file__).resolve().parents[1] / 'knowledge_contracts.py'
        source = path.read_text('utf-8')
        guard = 'envelope is not None and deadline < monotonic()'
        self.assertIn(guard, source)
        self.assertEqual(model_receipt_violations(ast.parse(source)), [])
        equivalent = source.replace(guard, 'deadline < monotonic() and envelope is not None')
        self.assertEqual(model_receipt_violations(ast.parse(equivalent)), [])
        for mutation in ('deadline < monotonic()', 'envelope and deadline < monotonic()',
                         'envelope is None and deadline < monotonic()',
                         'envelope is not None or deadline < monotonic()',
                         'envelope is not None and deadline > monotonic()'):
            with self.subTest(mutation=mutation):
                self.assertTrue(model_receipt_violations(ast.parse(source.replace(guard, mutation))))


class ModelCapacityOutcomeContracts(unittest.TestCase):
    def test_public_capacity_refusal_preserves_zero_calls_in_turn_trace_and_failure_event(self):
        from kb_platform.agent_call_budget import CallBudget
        from .knowledge_capability_contracts import KnowledgeCapabilityContracts
        from .product_fixture import Authority, Gateway, knowledge_service

        client, model, provider = KnowledgeCapabilityContracts.model(self)
        request = {'messages': [{'role': 'user', 'content': 'Synthetic capacity fixture'}],
                   'role': 'answer', 'schema': {'type': 'object'}, 'call_context': {}, 'output_limit': 16}
        for _ in range(64):
            client._dependency.complete(request)
        before = model.ledger.snapshot()
        authority = Authority([('Synthetic.md', 'Synthetic original evidence.')])
        with TemporaryDirectory() as directory:
            service = knowledge_service(None, client, root=Path(directory), gateway=Gateway(authority))
            try:
                session = service.create(authority.actor, authority.scope)
                result = service.turn(authority.actor, session['session_id'], message='What is the evidence?',
                                      request_id='capacity-turn', expected_revision=0)
                self.assertEqual(result['stop_reason'], 'MODEL_VALIDATION_CAPACITY')
                self.assertEqual(provider._post.call_count, 64)
                self.assertEqual(model.ledger.snapshot(), before)
                self.assertIsNone(model.turn_budget_snapshot(session['session_id'], 'capacity-turn'))
                calls = result['trace']['calls']
                events = service.store.events(authority.actor, session['session_id'])
                failed = [event['payload'] for event in events if event['kind'] == 'model_failed']
                self.assertEqual(len(calls), 1)
                self.assertEqual(failed, calls)
                self.assertEqual(sum(row.get('attempts', 1) for row in calls), 0)
                self.assertTrue(CallBudget(4, False, max_attempts=2).can_explore(calls))
                self.assertIs(calls[0]['model_sent'], False)
                self.assertEqual(calls[0]['attempts'], 0)
            finally:
                service.store.close()

    def test_capacity_no_send_lint_rejects_missing_or_unknown_outcome(self):
        from scripts.check_project import model_capacity_outcome_violations

        source = (Path(__file__).resolve().parents[1] / 'knowledge_contracts.py').read_text('utf-8')
        self.assertEqual(model_capacity_outcome_violations(ast.parse(source)), [])
        for old, new in (("'model_sent': False", "'model_sent': None"),
                         ("'attempts': 0", "'attempts': 1"),
                         ("'call_record':", "'ignored_record':")):
            with self.subTest(mutation=new):
                self.assertIn(old, source)
                self.assertTrue(model_capacity_outcome_violations(ast.parse(source.replace(old, new))))


class ModelReceiptFormalGateContracts(unittest.TestCase):
    def test_formal_registry_rejects_guard_mutants_even_after_source_digest_refresh(self):
        from .formal_state_machine_contracts import FormalRegistryContracts
        from .formal_state_machines import registry_violations, source_digest

        source = ('class ModelEndpoint:\n'
                  '    def complete(self, request):\n'
                  '        expired = [key for key, (deadline, envelope) in self._pending.items()\n'
                  '                   if envelope is not None and deadline < monotonic()]\n'
                  '        if len(self._pending) >= 64:\n'
                  "            raise ContractError('MODEL_VALIDATION_CAPACITY', 'capacity',\n"
                  "                details={'model_sent': False, 'call_record': {'attempts': 0, 'model_sent': False}})\n"
                  '        try:\n'
                  '            return self._model.complete(**request)\n'
                  '        except BaseException as error:\n'
                  '            with self._lock:\n'
                  '                self._pending.pop(token, None)\n'
                  '            if not isinstance(error, Exception):\n'
                  '                raise\n'
                  '            from .model_errors import error_details\n'
                  '            details = error_details(error)\n'
                  "            if not hasattr(error, 'code'):\n"
                  "                raise ContractError('CONTRACT_IMPLEMENTATION_FAILED', 'Service implementation failed',\n"
                  "                    details={**details, 'code': 'CONTRACT_IMPLEMENTATION_FAILED'}) from error\n")
        for mutation in (None, 'expiry', 'unknown', 'cancellation'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = FormalRegistryContracts().fixture(root)
                changed = source
                if mutation == 'expiry':
                    changed = source.replace('envelope is not None and ', '')
                if mutation == 'unknown':
                    changed = source.replace("'model_sent': False", "'model_sent': None")
                if mutation == 'cancellation':
                    changed = source.replace('except BaseException as error:', 'except Exception as error:')
                (root / 'kb_platform/knowledge_contracts.py').write_text(changed, encoding='utf-8')
                binding = {'path': 'kb_platform/knowledge_contracts.py', 'language': 'python',
                           'symbol': 'ModelEndpoint.complete'}
                binding['sha256'] = source_digest(root, binding)
                registry['machines'][0]['source_bindings'].append(binding)
                errors = registry_violations(root, registry)
                if mutation is None:
                    self.assertEqual(errors, [])
                else:
                    self.assertTrue(any(error.startswith('model receipts:') for error in errors), errors)
                    self.assertFalse(any('source review drift' in error for error in errors), errors)


class ModelReceiptCancellationContracts(unittest.TestCase):
    def test_cleanup_lint_rejects_exception_only_missing_cleanup_and_converted_cancellation(self):
        from scripts.check_project import model_receipt_cleanup_violations

        source = (Path(__file__).resolve().parents[1] / 'knowledge_contracts.py').read_text('utf-8')
        self.assertEqual(model_receipt_cleanup_violations(ast.parse(source)), [])
        for old, new in (('except BaseException as error:', 'except Exception as error:'),
                         ('self._pending.pop(token, None)', 'self._pending.get(token, None)'),
                         ('if not isinstance(error, Exception):', 'if False:'),
                         ('if not isinstance(error, Exception):', "if not hasattr(error, 'code'):")):
            with self.subTest(mutation=new):
                self.assertIn(old, source)
                self.assertTrue(model_receipt_cleanup_violations(ast.parse(source.replace(old, new))))

    def test_real_host_propagates_cancellation_identity_releases_slot_and_keeps_unknown_cost(self):
        from .knowledge_capability_contracts import KnowledgeCapabilityContracts

        class SyntheticCancellation(BaseException):
            code = 'SYNTHETIC_CANCEL'

        _, model, provider = KnowledgeCapabilityContracts.model(self)
        endpoint = ModelEndpoint(model)
        public = ServiceEndpoint(endpoint, MODEL_METHODS)
        success = provider._post.side_effect
        signals = []

        def cancel(*args, trace, **kwargs):
            trace['attempt'] += 1
            signal = SyntheticCancellation('controlled cancellation')
            signals.append(signal)
            raise signal

        provider._post.side_effect = cancel

        def mount(ctx, config):
            ctx.defer(endpoint.close)
            ctx.provide('model', public)

        profile = {'version': 1, 'plugins': [{'id': 'model', 'use': 'synthetic.model'}]}
        spec = PluginSpec('synthetic.model', ('model',), (), mount)
        request = {'messages': [{'role': 'user', 'content': 'Synthetic cancellation fixture'}],
                   'role': 'answer', 'schema': {'type': 'object'}, 'call_context': {}, 'output_limit': 16}
        now = [0]
        with patch('kb_platform.knowledge_contracts.monotonic', side_effect=lambda: now[0]), \
                PluginHost(profile, (spec,)) as host:
            for _ in range(64):
                with self.assertRaises(SyntheticCancellation) as caught:
                    host.call('model', 'complete', request)
                self.assertIs(caught.exception, signals[-1])
                self.assertEqual(caught.exception.code, 'SYNTHETIC_CANCEL')
            now[0] = 901
            self.assertEqual(provider._post.call_count, 64)
            self.assertEqual(host.inspect()['state'], 'ready')
            self.assertTrue(host.call('model', 'metadata', {})['policy'])
            self.assertEqual(endpoint._pending, {})
            failed_budget = model.ledger.snapshot()
            self.assertEqual(failed_budget['calls'], 64)
            self.assertEqual(failed_budget['unknown_usage_calls'], 64)
            self.assertGreater(failed_budget['accounted_tokens'], 0)
            self.assertEqual(failed_budget['reserved_calls'], 0)
            provider._post.side_effect = success
            response = host.call('model', 'complete', request)
            self.assertEqual(response['completion']['usage']['total_tokens'], 15)
            self.assertEqual(response['completion']['attempts'], 1)
            self.assertEqual(provider._post.call_count, 65)
            host.call('model', 'settle', {'token': response['token'], 'accept': True})
            provider._cache_put.assert_called_once()
            after = model.ledger.snapshot()
            self.assertEqual(after['calls'], 65)
            self.assertEqual(after['unknown_usage_calls'], 64)
            self.assertEqual(after['accounted_tokens'], failed_budget['accounted_tokens'] + 15)
            self.assertEqual(endpoint._pending, {})

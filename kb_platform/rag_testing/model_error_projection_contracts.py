import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from kb_platform.knowledge_contracts import MODEL_METHODS, ModelClient, ModelEndpoint
from kb_platform.model_errors import RemoteModelError
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.session_identity import PlatformError

from . import knowledge_capability_contracts as fixtures


class ModelErrorProjectionContracts(unittest.TestCase):
    canary = 'synthetic-private-diagnostic-canary'

    def model(self):
        _, model, provider = fixtures.KnowledgeCapabilityContracts.model(self)
        endpoint = ModelEndpoint(model)
        public = ServiceEndpoint(endpoint, MODEL_METHODS)

        def mount(ctx, config):
            ctx.defer(endpoint.close)
            ctx.provide('model', public)

        host = PluginHost({'version': 1, 'plugins': [{'id': 'model', 'use': 'synthetic.model'}]},
                          (PluginSpec('synthetic.model', ('model',), (), mount),))
        self.addCleanup(host.close)
        captured = []

        def exchange(method, request):
            try:
                return json.loads(json.dumps(host.call('model', method, json.loads(json.dumps(request)))))
            except ContractError as error:
                payload = json.loads(json.dumps(error.to_dict()))
                captured.append(payload)
                raise ContractError(**payload) from None

        client = ModelClient(SimpleNamespace(**{
            method: lambda request, method=method: exchange(method, request) for method in MODEL_METHODS}))
        return client, model, provider, endpoint, captured

    def options(self):
        return {'role': 'answer', 'schema': {'type': 'object', 'required': ['missing']},
                'output_limit': 16, 'call_context': {'session_id': 'synthetic-session',
                    'turn_id': 'synthetic-turn', 'max_physical_calls': 1}}

    def complete(self, model):
        return model.complete([{'role': 'user', 'content': 'Synthetic error projection'}], **self.options())

    def record_failure(self, provider):
        failure = OSError(self.canary)
        failure.__cause__ = RuntimeError(self.canary + '-cause')
        failure.private_diagnostic = self.canary + '-attribute'
        provider._record = Mock(side_effect=failure)
        return failure

    def assert_generic(self, error, captured):
        self.assertIsInstance(error, PlatformError)
        self.assertEqual(error.code, 'CONTRACT_IMPLEMENTATION_FAILED')
        self.assertEqual(error.status_code, 500)
        self.assertEqual(str(error), 'Service implementation failed')
        self.assertEqual(len(captured), 1)
        self.assertNotIn(self.canary, json.dumps(captured))
        self.assertNotIn('private_diagnostic', json.dumps(captured))
        self.assertNotIn('kind', error.details)

    def test_direct_and_actual_host_public_errors_preserve_observed_usage_without_private_diagnostics(self):
        for public in (False, True):
            with self.subTest(public=public):
                client, model, provider, endpoint, captured = self.model()
                failure = self.record_failure(provider)
                with self.assertRaises(PlatformError if public else OSError) as caught:
                    self.complete(client if public else model)
                error = caught.exception
                if public:
                    self.assert_generic(error, captured)
                else:
                    self.assertIs(error, failure)
                    self.assertEqual(captured, [])
                record = getattr(error, 'call_record', {})
                self.assertEqual(record.get('attempts'), 1)
                self.assertEqual(record.get('usage', {}).get('total_tokens'), 15)
                self.assertIs(getattr(error, 'model_sent', None), True)
                for budget in (model.ledger.snapshot(), model.turn_budget_snapshot('synthetic-session', 'synthetic-turn')):
                    self.assertEqual((budget['calls'], budget['tokens'], budget['unknown_usage_calls']), (1, 15, 0))
                    self.assertEqual((budget['reserved_calls'], budget['reserved_tokens']), (0, 0))
                self.assertEqual(provider._post.call_count, 1)
                self.assertEqual(endpoint._pending, {})

    def test_unknown_provider_failure_keeps_unknown_usage_without_inventing_send_certainty(self):
        client, model, provider, endpoint, captured = self.model()

        def fail(*args, trace, **kwargs):
            trace['attempt'] += 1
            raise RuntimeError(self.canary)

        provider._post.side_effect = fail
        with self.assertRaises(PlatformError) as caught:
            self.complete(client)
        error = caught.exception
        self.assert_generic(error, captured)
        record = getattr(error, 'call_record', {})
        self.assertEqual(record.get('attempts'), 1)
        self.assertIs(record.get('usage', {}).get('available'), False)
        self.assertIsNone(getattr(error, 'model_sent', None))
        self.assertIsNone(error.details.get('observed_usage'))
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 1)
        self.assertEqual(endpoint._pending, {})

    def test_observed_zero_and_proven_unsent_are_not_converted_to_unknown_or_one_attempt(self):
        for sent in (False, True):
            with self.subTest(sent=sent):
                client, model, provider, endpoint, captured = self.model()
                original = provider._post.side_effect

                def post(*args, trace, **kwargs):
                    if not sent:
                        failure = OSError(self.canary)
                        failure.model_sent = False
                        raise failure
                    result = original(*args, trace=trace, **kwargs)
                    result['usage'] = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
                    return result

                provider._post.side_effect = post
                self.record_failure(provider)
                with self.assertRaises(PlatformError) as caught:
                    self.complete(client)
                error = caught.exception
                self.assert_generic(error, captured)
                self.assertIs(error.model_sent, sent)
                self.assertEqual(error.call_record['attempts'], int(sent))
                self.assertIs(error.call_record['usage']['available'], sent)
                self.assertEqual(error.call_record['usage']['total_tokens'], 0 if sent else None)
                self.assertEqual(model.ledger.snapshot()['calls'], int(sent))
                self.assertEqual(model.ledger.snapshot()['tokens'], 0)
                self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 0)
                self.assertEqual(endpoint._pending, {})

    def test_unobserved_early_failure_does_not_fabricate_model_outcome(self):
        client, model, provider, endpoint, captured = self.model()
        provider._cache_get.side_effect = OSError(self.canary)
        with self.assertRaises(PlatformError) as caught:
            self.complete(client)
        self.assert_generic(caught.exception, captured)
        self.assertIsNone(getattr(caught.exception, 'call_record', None))
        self.assertIsNone(getattr(caught.exception, 'model_sent', None))
        self.assertIsNone(caught.exception.details.get('observed_usage'))
        self.assertEqual(model.ledger.snapshot()['calls'], 0)
        provider._post.assert_not_called()
        self.assertEqual(endpoint._pending, {})

    def test_coded_model_failure_retains_existing_model_error_projection(self):
        client, model, provider, endpoint, captured = self.model()
        with self.assertRaises(RemoteModelError) as caught:
            self.complete(client)
        self.assertEqual(caught.exception.code, 'INVALID_AGENT_OUTPUT')
        self.assertEqual(caught.exception.call_record['usage']['total_tokens'], 15)
        self.assertIs(caught.exception.model_sent, True)
        self.assertEqual(captured[0]['status_code'], 502)
        self.assertEqual(captured[0]['details']['kind'], 'model')
        self.assertEqual(model.ledger.snapshot()['tokens'], 15)
        self.assertEqual(endpoint._pending, {})

    def test_actual_turn_trace_and_failed_event_retain_known_usage(self):
        from .product_fixture import Authority, Gateway, knowledge_service

        client, model, provider, _, captured = self.model()
        self.record_failure(provider)
        authority = Authority([('Synthetic.md', 'Synthetic original evidence.')])
        with TemporaryDirectory() as directory:
            service = knowledge_service(None, client, root=Path(directory), gateway=Gateway(authority))
            try:
                session = service.create(authority.actor, authority.scope)
                result = service.turn(authority.actor, session['session_id'], message='What is the evidence?',
                                      request_id='projection-turn', expected_revision=0)
                self.assertEqual(result['stop_reason'], 'CONTRACT_IMPLEMENTATION_FAILED')
                calls = result['trace']['calls']
                failed = [event['payload'] for event in service.store.events(authority.actor, session['session_id'])
                          if event['kind'] == 'model_failed']
                self.assertEqual(failed, calls)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].get('attempts'), 1)
                self.assertEqual(calls[0].get('usage', {}).get('total_tokens'), 15)
                self.assertIs(calls[0].get('model_sent'), True)
                self.assertNotIn(self.canary, json.dumps([result, failed, captured]))
                self.assertEqual(model.ledger.snapshot()['tokens'], 15)
                self.assertEqual(provider._post.call_count, 1)
            finally:
                service.store.close()


class ModelErrorProjectionLintContracts(unittest.TestCase):
    def test_mandatory_lint_rejects_fact_loss_and_private_error_disclosure_after_digest_refresh(self):
        from scripts.check_project import model_error_projection_violations
        from .formal_state_machine_contracts import FormalRegistryContracts
        from .formal_state_machines import registry_violations, source_digest

        source = (Path(__file__).resolve().parents[1] / 'knowledge_contracts.py').read_text('utf-8')
        owner = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
                     and node.name == 'ModelEndpoint')
        selected = ast.unparse(owner)
        mutations = (("details = error_details(error)", 'details = {}'),
                     ("'Service implementation failed'", 'str(error)'),
                     ("{**details, 'code': 'CONTRACT_IMPLEMENTATION_FAILED'}", 'details'),
                     ("{**details, 'code': 'CONTRACT_IMPLEMENTATION_FAILED'}",
                      "{**details, 'code': 'CONTRACT_IMPLEMENTATION_FAILED', 'model_sent': False}"))
        for mutation in (None, *mutations):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = FormalRegistryContracts().fixture(root)
                changed = selected if mutation is None else selected.replace(*mutation)
                if mutation is not None:
                    self.assertNotEqual(changed, selected)
                path = root / 'kb_platform/knowledge_contracts.py'
                path.write_text(changed, encoding='utf-8')
                binding = {'path': 'kb_platform/knowledge_contracts.py', 'language': 'python',
                           'symbol': 'ModelEndpoint'}
                binding['sha256'] = source_digest(root, binding)
                registry['machines'][0]['source_bindings'].append(binding)
                errors = registry_violations(root, registry)
                if mutation is None:
                    self.assertEqual(errors, [])
                    self.assertEqual(model_error_projection_violations(ast.parse(changed)), [])
                else:
                    self.assertTrue(model_error_projection_violations(ast.parse(changed)))
                    self.assertTrue(any('generic model failures' in error for error in errors), errors)
                    self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()

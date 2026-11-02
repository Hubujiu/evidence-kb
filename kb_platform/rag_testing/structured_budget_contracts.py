from types import SimpleNamespace
import unittest

from kb_platform.knowledge_contracts import MODEL_METHODS, ModelClient
from kb_platform.model_errors import RemoteModelError
from kb_platform.model_service import Capability, ModelInfo, ModelService

from . import contract_application_contracts as application_fixtures
from .harness_context_model_contracts import _Provider


class StructuredBudgetContracts(unittest.TestCase):
    def test_production_model_boundary_reports_known_and_unknown_turn_cost_without_replay(self):
        for usage_available in (True, False):
            with self.subTest(usage_available=usage_available):
                fixture = application_fixtures.ContractApplicationContracts()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                if not usage_available:
                    original = fixture.client._post

                    def post_without_usage(*args, **kwargs):
                        response = original(*args, **kwargs)
                        response.pop('usage')
                        return response

                    fixture.client._post = post_without_usage
                composition = fixture.compose()
                scope, _ = fixture.publish(composition)
                application = composition.application
                session = application.create(fixture.actor, scope)
                contract = application.composition
                model = ModelClient(SimpleNamespace(**{
                    method: lambda request, method=method: contract.call('model', 'model', method, request)
                    for method in MODEL_METHODS}))
                request = {'message': 'What is the original rule?', 'request_id': 'budget-boundary',
                           'expected_revision': 0}
                self.assertIsNone(model.turn_budget_snapshot(session['session_id'], request['request_id']))
                result = application.turn(fixture.actor, session['session_id'], **request)
                self.assertEqual(result['status'], 'answered', result)
                snapshot = model.turn_budget_snapshot(session['session_id'], request['request_id'])
                self.assertEqual(snapshot['calls'], 4)
                self.assertEqual(snapshot['reserved_calls'], 0)
                self.assertEqual(snapshot['reserved_tokens'], 0)
                self.assertEqual(snapshot['tokens'], 60 if usage_available else 0)
                self.assertEqual(snapshot['unknown_usage_calls'], 0 if usage_available else 4)
                self.assertGreater(snapshot['accounted_tokens'], 0)
                self.assertEqual(snapshot['accounted_tokens'], model.ledger.snapshot()['accounted_tokens'])
                self.assertIsNone(model.turn_budget_snapshot(session['session_id'], 'different-turn'))
                self.assertEqual(application.turn(fixture.actor, session['session_id'], **request), result)
                self.assertEqual(model.turn_budget_snapshot(session['session_id'], request['request_id']), snapshot)
                self.assertEqual(len(fixture.physical), 4)

    def test_turn_budget_read_rejects_a_revoked_model_registration(self):
        service = ModelService()
        self.addCleanup(service.close)
        provider = _Provider([])
        dispose = service.register('generation', SimpleNamespace(
            info=ModelInfo('synthetic', 'controlled', frozenset({Capability.CHAT})),
            invoke=lambda request: self.fail('Reading a budget must not invoke the model'),
            completion_port=lambda: provider))
        model = service.structured('generation')
        self.assertIsNone(model.turn_budget_snapshot('session', 'turn'))
        dispose()
        with self.assertRaises(RemoteModelError) as caught:
            model.turn_budget_snapshot('session', 'turn')
        self.assertEqual(caught.exception.code, 'MODEL_ROUTE_UNAVAILABLE')
        self.assertEqual(provider.calls, [])

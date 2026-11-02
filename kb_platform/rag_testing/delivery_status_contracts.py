from contextlib import closing, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.delivery import RUNTIME_CAPS, WINDOW, _budget_status, create_delivery_app

from .budget import BudgetLedger
from .budget_batches import ReviewedPredecessorLedger, freeze_predecessor


CAPS = {'model': 4, 'input_tokens': 1000, 'output_tokens': 100,
        'remote_embedding_calls': 3, 'embedding_input_tokens': 1000}
RESERVED = {'model': 1, 'input_tokens': 40, 'output_tokens': 20,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0}
OBSERVED = {**RESERVED, 'input_tokens': 17, 'output_tokens': 3}


@contextmanager
def unavailable_calls(path):
    with closing(sqlite3.connect(path)) as database, database:
        database.execute('ALTER TABLE calls RENAME TO unavailable_calls')
    try:
        yield
    finally:
        with closing(sqlite3.connect(path)) as database, database:
            database.execute('ALTER TABLE unavailable_calls RENAME TO calls')


@contextmanager
def unavailable_predecessor(path):
    original = Path(path)
    hidden = original.with_suffix('.unavailable')
    original.rename(hidden)
    try:
        yield
    finally:
        hidden.rename(original)


class BudgetStatusFixture:
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def ledger(self, name='current'):
        ledger = BudgetLedger(self.root / name / 'physical-calls.sqlite3', CAPS)
        self.addCleanup(ledger.close)
        return ledger

    def settle(self, ledger, request_id, *, status='COMPLETE'):
        ledger.reserve(request_id, RESERVED)
        ledger.finish(request_id, status=status, usage=OBSERVED if status == 'COMPLETE' else None)

    def linked(self):
        parent = self.ledger('parent')
        self.settle(parent, 'parent-complete')
        frozen = freeze_predecessor(parent.path)
        parent.close()
        child = ReviewedPredecessorLedger(self.root / 'child' / 'physical-calls.sqlite3', CAPS,
                                          predecessors=[frozen])
        self.addCleanup(child.ledger.close)
        return parent, child

    def assert_unavailable(self, status, reason='BUDGET_READ_UNAVAILABLE'):
        self.assertEqual(status['status'], 'BLOCKED')
        self.assertEqual(status['budget_status'], 'BLOCKED')
        self.assertEqual(status['blocked_reason'], reason)
        self.assertFalse(status['usage_available'])
        self.assertFalse(status['cumulative_usage_available'])
        for key in ('effective_usage_or_reserved', 'remaining', 'current_pending_or_unknown',
                    'pending_or_unknown', 'unknown_usage_requests',
                    'cumulative_unknown_usage_requests', 'cumulative_effective_usage_or_reserved'):
            self.assertIsNone(status[key], key)


class DeliveryStatusContracts(BudgetStatusFixture, unittest.TestCase):
    def test_plain_read_failure_reports_unknown_then_recovers_original_facts(self):
        ledger = self.ledger()
        self.settle(ledger, 'already-complete')
        before = ledger.summary()
        with unavailable_calls(ledger.path):
            self.assert_unavailable(_budget_status(ledger))
            self.assertIsNone(ledger.status_snapshot()['current'])
        status = _budget_status(ledger)
        self.assertEqual(status['status'], 'AVAILABLE')
        self.assertTrue(status['usage_available'])
        self.assertEqual(status['effective_usage_or_reserved'], OBSERVED)
        self.assertEqual(status['remaining']['model'], CAPS['model'] - 1)
        self.assertEqual(ledger.summary(), before)

    def test_current_batch_read_failure_never_uses_parent_totals_as_current_usage(self):
        _, ledger = self.linked()
        self.settle(ledger, 'current-complete')
        before = ledger.summary()
        with unavailable_calls(ledger.path):
            self.assert_unavailable(_budget_status(ledger))
        self.assertEqual(ledger.summary(), before)
        status = _budget_status(ledger)
        self.assertEqual(status['effective_usage_or_reserved'], OBSERVED)
        self.assertEqual(status['cumulative_effective_usage_or_reserved']['model'], 2)

    def test_unreadable_history_preserves_current_usage_and_unknown_reservations(self):
        parent, ledger = self.linked()
        self.settle(ledger, 'current-unknown', status='UNKNOWN')
        with unavailable_predecessor(parent.path):
            status = _budget_status(ledger)
            self.assertEqual(status['status'], 'BLOCKED')
            self.assertEqual(status['blocked_reason'], 'PREDECESSOR_LEDGER_UNAVAILABLE')
            self.assertTrue(status['usage_available'])
            self.assertFalse(status['cumulative_usage_available'])
            self.assertEqual(status['effective_usage_or_reserved'], RESERVED)
            self.assertEqual(status['remaining']['output_tokens'], CAPS['output_tokens'] - RESERVED['output_tokens'])
            self.assertEqual(status['current_pending_or_unknown'], 1)
            self.assertEqual(status['unknown_usage_requests'], 1)
            for key in ('pending_or_unknown', 'reviewed_unknown_requests',
                        'cumulative_unknown_usage_requests', 'cumulative_effective_usage_or_reserved'):
                self.assertIsNone(status[key], key)
        restored = _budget_status(ledger)
        self.assertEqual(restored['pending_or_unknown'], 1)
        self.assertEqual(restored['blocked_reason'], 'UNRESOLVED_PHYSICAL_REQUEST')

    def test_changed_history_is_blocked_without_losing_current_facts(self):
        parent, ledger = self.linked()
        self.settle(ledger, 'current-complete')
        with closing(sqlite3.connect(parent.path)) as database, database:
            database.execute("UPDATE calls SET diagnostic='changed after freeze'")
        status = _budget_status(ledger)
        self.assertEqual(status['blocked_reason'], 'PREDECESSOR_LEDGER_CHANGED')
        self.assertEqual(status['status'], 'BLOCKED')
        self.assertTrue(status['usage_available'])
        self.assertEqual(status['effective_usage_or_reserved'], OBSERVED)
        self.assertEqual(status['current_pending_or_unknown'], 0)
        self.assertIsNone(status['pending_or_unknown'])

    def test_known_zero_exhaustion_and_provider_block_remain_distinct(self):
        ledger = self.ledger()
        status = _budget_status(ledger)
        self.assertEqual(status['status'], 'AVAILABLE')
        self.assertTrue(status['usage_available'])
        self.assertEqual(status['effective_usage_or_reserved'], dict.fromkeys(CAPS, 0))
        self.assertEqual(status['pending_or_unknown'], 0)
        provider = _budget_status(ledger, provider_blocked=True)
        self.assertEqual(provider['status'], 'BLOCKED')
        self.assertEqual(provider['budget_status'], 'AVAILABLE')
        self.assertEqual(provider['blocked_reason'], 'PROVIDER_PERMISSION_BLOCKED')
        ledger.reserve('all-model-calls', {**dict.fromkeys(CAPS, 0), 'model': CAPS['model']})
        ledger.finish('all-model-calls', status='COMPLETE',
                      usage={**dict.fromkeys(CAPS, 0), 'model': CAPS['model']})
        exhausted = _budget_status(ledger)
        self.assertEqual(exhausted['status'], 'BUDGET_EXHAUSTED')
        self.assertEqual(exhausted['remaining']['model'], 0)
        self.assertTrue(exhausted['usage_available'])

    def test_reviewed_history_retains_unknown_usage_without_reblocking_authorized_batch(self):
        from .reviewed_predecessor_contracts import ReviewedPredecessorContracts

        ReviewedPredecessorContracts.setUp(self)
        self.addCleanup(self.old.close)
        frozen = ReviewedPredecessorContracts.freeze(self)
        ledger = ReviewedPredecessorLedger(self.root / 'current.sqlite3', self.caps, predecessors=[frozen])
        self.addCleanup(ledger.ledger.close)
        status = _budget_status(ledger)
        self.assertEqual(status['status'], 'AVAILABLE')
        self.assertEqual(status['current_pending_or_unknown'], 0)
        self.assertEqual(status['pending_or_unknown'], 1)
        self.assertEqual(status['reviewed_unknown_requests'], 1)
        self.assertEqual(status['unknown_usage_requests'], 0)
        self.assertEqual(status['cumulative_unknown_usage_requests'], 1)
        self.assertEqual(status['cumulative_effective_usage_or_reserved'], self.reserved)
        self.assertEqual(self.old.entry('unknown')['status'], 'UNKNOWN')


def live_suite():
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient
    import httpx

    from kb_platform.agent_api import install_agent_routes

    class DeliveryStatusIntegration(BudgetStatusFixture, unittest.TestCase):
        def setUp(self):
            super().setUp()
            environment = patch.dict(os.environ,
                {key: value for key, value in os.environ.items() if not key.startswith('KB_')}, clear=True)
            environment.start()
            self.addCleanup(environment.stop)
            self.run = self.root / 'run'
            self.run.mkdir()
            self.physical = []
            self.config_path = self.root / 'profile.json'
            self.providers_path = self.run / 'providers.json'
            profile = {'protocol': 'enterprise-kb-delivery/1', 'id': 'delivery-status-contract',
                'models': {'generation': 'codex/gpt-6-luna',
                    'embedding': 'google_gemini:gemini-embedding-2:768', 'model_comparison': False},
                'window': deepcopy(WINDOW), 'scope': {'cross_session_memory': False},
                'admission': {'concurrent_model_requests': 1}, 'physical_call_caps': dict(CAPS)}
            providers = {'luna': {'base_url': 'https://status-fixture.invalid/v1',
                    'api_key': 'fixture-key', 'model': 'codex/gpt-6-luna', 'cache_mode': 'disabled', 'retries': 0},
                'google': {'base_url': 'https://status-fixture.invalid/v1beta', 'api_key': 'fixture-key',
                    'model': 'gemini-embedding-2', 'dimension': 768, 'cache_mode': 'disabled',
                    'trust_env': False, 'retries': 0},
                'cache_dir': str(self.run / 'cache'), 'log_path': str(self.run / 'calls.jsonl')}
            self.config_path.write_text(json.dumps(profile), 'utf-8')
            self.providers_path.write_text(json.dumps(providers), 'utf-8')

        def application(self, *, batch=None):
            def construct(settings, *, model_clients):
                _, embedding = model_clients.values
                application = FastAPI()
                platform = SimpleNamespace(embedding_provider=embedding)
                research = SimpleNamespace(embedding=embedding)
                agent = SimpleNamespace(model=SimpleNamespace(policy=SimpleNamespace(model_window=WINDOW['total'],
                    input_budget=WINDOW['input'], output_reserve=WINDOW['output'], safety_reserve=WINDOW['safety'])))
                identities = settings.identities()

                def identity(authorization: str | None = Header(default=None)):
                    actor = identities.get((authorization or '').removeprefix('Bearer '))
                    if actor is None:
                        raise HTTPException(401, 'Authentication required')
                    return actor

                application.state.platform, application.state.research = platform, research
                install_agent_routes(application, platform, research, identity, application=agent)
                return application

            def unexpected(request):
                self.physical.append(str(request.url))
                raise AssertionError('STATUS_MUST_NOT_SEND_PROVIDER_REQUESTS')

            app = create_delivery_app(self.config_path, self.providers_path, self.run,
                app_factory=construct, budget_batch=batch,
                inner_factory=lambda provider, category: httpx.MockTransport(unexpected))
            token = next(iter(json.loads((self.run / 'identities.json').read_text('utf-8'))))
            return app, {'Authorization': 'Bearer ' + token}

        def request_status(self, client, headers):
            response = client.get('/agent/runtime-status', headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['X-Delivery-Profile'], 'delivery-status-contract')
            value = response.json()
            self.assertEqual(value['model_calls_from_status'], 0)
            self.assertEqual(self.physical, [])
            return value

        def test_http_plain_read_failure_is_blocked_json_without_provider_calls(self):
            app, headers = self.application()
            ledger = app.state.delivery['ledger']
            self.addCleanup(ledger.close)
            self.settle(ledger, 'already-complete')
            before = ledger.summary()
            with TestClient(app) as client:
                with unavailable_calls(ledger.path):
                    self.assert_unavailable(self.request_status(client, headers))
                    self.assert_unavailable(self.request_status(client, headers))
                recovered = self.request_status(client, headers)
                self.assertEqual(recovered['effective_usage_or_reserved'], OBSERVED)
                self.assertEqual(recovered['status'], 'AVAILABLE')
                self.assertEqual(client.get('/agent/runtime-status').status_code, 401)
            self.assertEqual(ledger.summary(), before)
            self.assertEqual(list(app.state.delivery['capture_root'].glob('*.json')), [])

        def test_http_batch_read_failures_preserve_only_observed_facts(self):
            predecessors = []
            for path in (self.run / 'physical-calls.sqlite3', self.root / 'semantic' / 'physical-calls.sqlite3'):
                ledger = BudgetLedger(path, CAPS)
                self.settle(ledger, path.parent.name + '-complete')
                predecessors.append(freeze_predecessor(path))
                ledger.close()
            batch_path = self.root / 'batch' / 'batch.json'
            batch_path.parent.mkdir()
            batch_path.write_text(json.dumps({'protocol': 'delivery-runtime-budget-batch/1',
                'id': 'runtime-status-contract', 'tenant_id': 'delivery-status-contract',
                'caps': RUNTIME_CAPS, 'predecessors': predecessors,
                'semantic_predecessor': predecessors[1]['path']}), 'utf-8')
            app, headers = self.application(batch=batch_path)
            ledger = app.state.delivery['ledger']
            self.addCleanup(ledger.ledger.close)
            self.settle(ledger, 'current-complete')
            before = ledger.summary()
            with TestClient(app) as client:
                with unavailable_predecessor(predecessors[0]['path']):
                    status = self.request_status(client, headers)
                    self.assertEqual(status['status'], 'BLOCKED')
                    self.assertEqual(status['blocked_reason'], 'PREDECESSOR_LEDGER_UNAVAILABLE')
                    self.assertTrue(status['usage_available'])
                    self.assertEqual(status['effective_usage_or_reserved'], OBSERVED)
                    self.assertEqual(status['remaining']['model'], RUNTIME_CAPS['model'] - 1)
                    self.assertEqual(status['current_pending_or_unknown'], 0)
                    self.assertIsNone(status['pending_or_unknown'])
                    self.assertIsNone(status['cumulative_effective_usage_or_reserved'])
                with unavailable_calls(ledger.path):
                    self.assert_unavailable(self.request_status(client, headers))
                restored = self.request_status(client, headers)
                self.assertEqual(restored['status'], 'AVAILABLE')
                self.assertEqual(restored['cumulative_effective_usage_or_reserved']['model'], 3)
            self.assertEqual(ledger.summary(), before)
            self.assertEqual(list(app.state.delivery['capture_root'].glob('*.json')), [])

    return unittest.defaultTestLoader.loadTestsFromTestCase(DeliveryStatusIntegration)

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from kb_platform.llm_contract import LlmClient, LlmEndpoint
from kb_platform.model_errors import ModelPreflightError
from kb_platform.model_http import OpenAICompatibleAdapter
from kb_platform.model_sdk import Capability, ModelRequest
from kb_platform.model_service import ModelService

from .budget import BudgetLedger
from .foundation_wire_fixture import LoopbackServer


class MutablePolicy:
    def __init__(self):
        self.allowed = True
        self.admissions = []

    def admit(self, request):
        self.admissions.append(request)
        return {'allowed': self.allowed}


class RevokingLedger(BudgetLedger):
    def __init__(self, path, policy):
        super().__init__(path, {'requests': 3, 'units': 30})
        self.policy, self.revoke_after_reserve = policy, False

    def reserve(self, *args, **kwargs):
        result = super().reserve(*args, **kwargs)
        if self.revoke_after_reserve:
            self.policy.allowed = False
        return result


class FoundationTransportContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.policy = MutablePolicy()
        self.ledger = RevokingLedger(self.root / 'physical.sqlite3', self.policy)
        self.addCleanup(self.ledger.close)

        def embedding(request):
            if request['path'] != '/v1/embeddings':
                raise ValueError('UNEXPECTED_EMBEDDING_ENDPOINT')
            value = json.loads(request['body'])
            if value['input'] != ['current authorized text']:
                raise ValueError('UNEXPECTED_EMBEDDING_INPUT')
            return 200, json.dumps({'data': [{'index': 0, 'embedding': [1.0, 0.0]}]}).encode(), {
                'Content-Type': 'application/json'}

        self.wire = LoopbackServer(embedding)
        self.addCleanup(self.wire.close)
        adapter = OpenAICompatibleAdapter(provider='local-embedding', model='fixture',
            endpoint=self.wire.url + '/v1', api_key='local-fixture-key', capabilities=[Capability.EMBEDDING],
            ledger=self.ledger, reservation={'requests': 1, 'units': 10}, audit_root=self.root / 'audit')
        self.models = ModelService()
        self.models.register('embedding', adapter, owned=True)
        self.addCleanup(self.models.close)
        endpoint = LlmEndpoint(self.models, metadata={'embedding': {'dimension': 2}}, policy=self.policy)
        self.addCleanup(endpoint.close)
        self.client = LlmClient(SimpleNamespace(catalog=endpoint.catalog, exchange=endpoint.exchange))
        self.request = ModelRequest(Capability.EMBEDDING,
            {'texts': ['current authorized text'], 'task': 'document', 'dimensions': 2},
            {'purpose': 'embedding_document', 'request_id': 'original-embedding-request'})

    def test_public_llm_policy_allows_actual_authorized_http_embedding(self):
        result = self.client.invoke('embedding', self.request)
        self.assertEqual(result.data['vectors'], [[1.0, 0.0]])
        self.assertEqual(len(self.wire.requests), 1)
        self.assertEqual(self.wire.errors, [])
        self.assertEqual(self.ledger.summary()['physical_requests'], 1)
        self.assertEqual(self.ledger.summary()['pending_or_unknown'], 0)
        self.assertTrue(self.policy.admissions)
        self.assertTrue(all(row['context']['request_id'] == 'original-embedding-request'
                            for row in self.policy.admissions))

    def test_revocation_during_real_reservation_prevents_http_and_retains_known_unsent_receipt(self):
        self.ledger.revoke_after_reserve = True
        with self.assertRaises(ModelPreflightError) as raised:
            self.client.invoke('embedding', self.request)
        self.assertEqual(raised.exception.code, 'MODEL_AUTHORIZATION_FAILED')
        self.assertIs(raised.exception.model_sent, False)
        self.assertEqual(self.wire.requests, [])
        snapshot = self.ledger.summary()
        self.assertEqual(snapshot['physical_requests'], 1)
        self.assertEqual(snapshot['pending_or_unknown'], 0)
        self.assertEqual(snapshot['effective_usage_or_reserved'], {'requests': 0, 'units': 0})
        records = [json.loads(path.read_text('utf-8')) for path in (self.root / 'audit').glob('*.json')]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'ERROR')
        self.assertEqual(records[0]['physical_request_id'], raised.exception.physical_request_id)
        self.assertEqual(records[0]['context']['request_id'], 'original-embedding-request')

    def test_preexisting_revocation_refuses_before_reservation(self):
        self.policy.allowed = False
        with self.assertRaises(ModelPreflightError) as raised:
            self.client.invoke('embedding', self.request)
        self.assertEqual(raised.exception.code, 'MODEL_AUTHORIZATION_FAILED')
        self.assertIs(raised.exception.model_sent, False)
        self.assertEqual(self.wire.requests, [])
        self.assertEqual(self.ledger.summary()['physical_requests'], 0)

    def test_http_runtime_status_remains_blocked_when_budget_storage_fails(self):
        import asyncio
        import httpx
        from fastapi import FastAPI
        from unittest.mock import Mock, patch
        from kb_platform.agent_api import install_agent_routes
        from kb_platform.database_contracts import DatabaseError
        from kb_platform.delivery import create_delivery_app
        from kb_platform.session_identity import Actor

        run = self.root / 'delivery'
        run.mkdir()
        config = {'protocol': 'enterprise-kb-delivery/1', 'id': 'delivery-status-fixture',
            'models': {'generation': 'codex/gpt-6-luna', 'embedding': 'google_gemini:gemini-embedding-2:768',
                       'model_comparison': False},
            'window': {'total': 131072, 'input': 118784, 'output': 4096, 'safety': 8192,
                       'counting': 'conservative_utf8_estimate'},
            'scope': {'cross_session_memory': False}, 'admission': {'concurrent_model_requests': 1},
            'physical_call_caps': {'model': 2, 'input_tokens': 1000, 'output_tokens': 100,
                                   'remote_embedding_calls': 2, 'embedding_input_tokens': 1000}}
        providers = {'luna': {'model': 'codex/gpt-6-luna', 'cache_mode': 'disabled', 'retries': 0},
            'google': {'model': 'gemini-embedding-2', 'dimension': 768, 'cache_mode': 'disabled', 'retries': 0},
            'cache_dir': str(run / 'cache'), 'log_path': str(run / 'calls.jsonl')}
        config_path, providers_path = run / 'config.json', run / 'providers.json'
        config_path.write_text(json.dumps(config), 'utf-8')
        providers_path.write_text(json.dumps(providers), 'utf-8')
        admission = SimpleNamespace(scope_hash='local', directory=run, snapshot=lambda **kwargs: {'state': 'AVAILABLE'})
        model = SimpleNamespace(provider_id='fixture', admission=admission, close=Mock())
        embedding = SimpleNamespace(provider_id='google_gemini', admission=admission, close=Mock())
        policy = SimpleNamespace(model_window=131072, input_budget=118784, output_reserve=4096, safety_reserve=8192)
        agent = SimpleNamespace(model=SimpleNamespace(policy=policy), semantic_pages=None)
        platform = SimpleNamespace(embedding_provider=embedding)
        research = SimpleNamespace(embedding=embedding, configured=True)
        actor = Actor('delivery-status-fixture', 'local')
        app = FastAPI()
        app.state.platform, app.state.research = platform, research
        install_agent_routes(app, platform, research, lambda: actor, application=agent)
        with patch('kb_platform.delivery.CapturedLuna', return_value=model), \
                patch('kb_platform.remote.GoogleEmbeddingProvider', return_value=embedding), \
                patch('kb_platform.delivery.observe_client'), patch('kb_platform.delivery._serialize_client_sends'):
            create_delivery_app(config_path, providers_path, run, app_factory=lambda settings, **kwargs: app)
        ledger = app.state.delivery['ledger']

        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://testserver') as client:
                return await client.get('/agent/runtime-status')

        try:
            with patch.object(ledger, 'summary', side_effect=DatabaseError('BUDGET_READ_UNAVAILABLE')):
                response = asyncio.run(request())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['status'], 'BLOCKED')
            self.assertEqual(response.json()['blocked_reason'], 'BUDGET_READ_UNAVAILABLE')
            self.assertIs(response.json()['usage_available'], False)
            self.assertIsNone(response.json()['remaining'])
            self.assertIsNone(response.json()['effective_usage_or_reserved'])
        finally:
            for close in app.router.on_shutdown:
                close()

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from kb_platform.application_knowledge import DEFAULT_APPLICATION, create_contract_knowledge
from kb_platform.application_storage import StorageClient
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.settings import Settings
from kb_platform.storage_catalog import initialize_configuration

from .agent_graph_contracts import cited_answer, decision, verdict
from .budget import BudgetLedger
from .foundation_wire_fixture import LocalModelWire, LocalS3Wire


ROOT = Path(__file__).resolve().parents[2]
CAPS = {'requests': 32, 'units': 320}
SOURCE = b'Original rule value 42.'
PROVIDERS = ['fixture-openai-compatible', 'fixture-anthropic']


class ContractDependency:
    def __init__(self, application, instance, export):
        self.application, self.instance, self.export = application, instance, export

    def __getattr__(self, method):
        return lambda request: self.application.call(self.instance, self.export, method, request)


class FoundationKnowledgeContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.actor = Actor('foundation-tenant', 'owner')
        self.token = 'foundation-local-identity-token-32-characters'
        self.identities = self.root / 'identities.json'
        self.identities.write_text(json.dumps({self.token: {
            'tenant_id': self.actor.tenant_id, 'principal_id': self.actor.principal_id}}), 'utf-8')
        self.key_env = 'FOUNDATION_FIXTURE_KEY_' + uuid4().hex.upper()
        os.environ[self.key_env] = 'local-fixture-credential'
        self.addCleanup(os.environ.pop, self.key_env, None)

    def case(self, name, *, adapter='openai-compatible', object_provider='files', responses=None):
        directory = self.root / name
        directory.mkdir()
        wire = LocalModelWire(adapter, [decision(), decision('answer'), cited_answer, verdict()]
                              if responses is None else responses)
        self.addCleanup(wire.close)
        storage = json.loads((ROOT / 'config/storage.local.example.json').read_text('utf-8'))
        storage['application_id'] = 'foundation-knowledge-' + name
        for instance, row in storage['instances'].items():
            row['config']['root'] = str(directory / instance)
        s3 = None
        if object_provider == 's3':
            s3 = LocalS3Wire()
            self.addCleanup(s3.close)
            storage['instances']['originals'] = {'provider': 's3', 'config': {
                'endpoint_url': s3.url, 'bucket': 'foundation-fixture',
                'access_key': 'local-fixture-access', 'secret_key': 'local-fixture-secret'}}
        storage['bindings']['store'] = {'primary': {
            'service': 'records.domain', 'owner': 'sessions', 'space': 'history'}}
        storage['bindings']['provider'] = {'primary': {
            'service': 'records.domain', 'owner': 'model-budget', 'space': 'ledger'}}
        storage = initialize_configuration(storage)
        storage_path, model_path = directory / 'storage.json', directory / 'models.json'
        storage_path.write_text(json.dumps(storage), 'utf-8')
        provider = 'fixture-' + adapter
        model_config = {'version': 1, 'budget': {
            'path': str(directory / 'unused-path-ledger.sqlite3'), 'caps': CAPS},
            'providers': {provider: {'adapter': adapter, 'base_url': wire.url + '/v1',
                'api_key_env': self.key_env, 'timeout_seconds': 3}},
            'models': {'generation': {'provider': provider, 'model': 'controlled-model',
                'capabilities': ['chat'], 'reservation': {'requests': 1, 'units': 10}}}}
        model_path.write_text(json.dumps(model_config), 'utf-8')
        profile = json.loads(DEFAULT_APPLICATION.read_text('utf-8'))
        profile['plugins']['storage']['config']['path'] = str(storage_path)
        profile['plugins']['llm']['config'] = {'config_path': str(model_path),
            'budget_binding': 'provider.primary', 'audit_root': str(directory / 'audit')}
        profile['plugins']['access']['config']['identities_file'] = str(self.identities)
        profile['plugins']['data']['config'].update(research_root=str(self.root / 'research'), rerank=False)
        profile['plugins']['workflow']['config']['retrieval_config'] = {
            'channels': ['bm25'], 'rerank': False, 'parent': 'none', 'title_navigation': False}
        return SimpleNamespace(profile=profile, settings=Settings(directory / 'store', self.identities, storage_path),
                               wire=wire, s3=s3, directory=directory, provider=provider)

    def compose(self, case):
        application = create_contract_knowledge(case.settings, profile=case.profile, environment={})
        self.addCleanup(application.close)
        return application

    def publish(self, composition):
        scope = composition.platform.create_kb(self.actor, 'Foundation acceptance')['kb_id']
        source = composition.platform.ingest(self.actor, scope, 'Original.md', SOURCE, policy={
            'llm_context': True, 'embedding': False, 'allowed_providers': PROVIDERS})
        build = composition.platform.stage(self.actor, scope, ['bm25'])
        composition.platform.publish(self.actor, scope, build['build_id'])
        return scope, source

    def snapshot(self, composition):
        dependency = ContractDependency(composition.application.composition, 'storage', 'storage')
        ledger = BudgetLedger(caps=CAPS, database=StorageClient(dependency, 'provider.primary').relational())
        try:
            return ledger.summary()
        finally:
            ledger.close()

    def test_same_knowledge_consumer_replaces_model_wire_and_object_provider_by_configuration(self):
        variants = [('compatible-files', 'openai-compatible', 'files'), ('anthropic-s3', 'anthropic', 's3')]
        business = None
        for name, adapter, object_provider in variants:
            with self.subTest(adapter=adapter, storage=object_provider):
                case = self.case(name, adapter=adapter, object_provider=object_provider)
                unchanged = deepcopy(case.profile)
                for service in ('llm', 'storage'):
                    unchanged['plugins'][service]['config'] = {}
                if business is None:
                    business = unchanged
                self.assertEqual(unchanged, business)
                composition = self.compose(case)
                self.assertEqual(composition.authenticate(self.token), self.actor)
                scope, source = self.publish(composition)
                self.assertEqual(source['document']['status'], 'READY')
                hits = composition.platform.search(self.actor, scope, 'Original rule')['hits']
                self.assertTrue(hits)
                self.assertEqual(composition.platform.read_original(self.actor, scope, hits[0]['citation_id']), SOURCE)
                application = composition.application
                session_id = application.create(self.actor, scope)['session_id']
                request = {'message': 'What is the original rule?', 'request_id': 'stable-question',
                           'expected_revision': 0, 'constraints': ['Use only the original rule']}
                result = application.turn(self.actor, session_id, **request)
                self.assertEqual(result['status'], 'answered', result)
                self.assertEqual(result['answer'], SOURCE.decode())
                self.assertTrue(result['claims'])
                self.assertIn(hits[0]['citation_id'], {row['citation_id'] for row in result['evidence']})
                self.assertEqual(len(case.wire.requests), 4)
                self.assertEqual(application.turn(self.actor, session_id, **request), result)
                self.assertEqual(len(case.wire.requests), 4)
                self.assertEqual(application.get(self.actor, session_id)['revision'], 1)
                self.assertEqual(application.model_status()['budget']['calls'], 4)
                self.assertEqual(self.snapshot(composition)['physical_requests'], 4)
                payload = json.loads(case.wire.requests[2]['body'])
                projection = json.loads(payload['messages'][-1]['content'])
                self.assertIn(SOURCE.decode(), [row.get('text') for row in projection['evidence']])
                headers = case.wire.requests[0]['headers']
                self.assertIn('x-api-key' if adapter == 'anthropic' else 'authorization', headers)
                with self.assertRaises(PlatformError):
                    application.get(Actor(self.actor.tenant_id, 'someone-else'), session_id)
                with self.assertRaises(PlatformError):
                    composition.platform.search(Actor('another-tenant', 'owner'), scope, 'Original')
                composition.platform.set_source_policy(self.actor, source['source_id'], {
                    'llm_context': False, 'embedding': False, 'allowed_providers': PROVIDERS})
                with self.assertRaises(PlatformError):
                    application.turn(self.actor, session_id, **request)
                self.assertEqual(len(case.wire.requests), 4)
                self.assertTrue(application.get(self.actor, session_id)['state']['history_redacted'])
                events = application.history(self.actor, session_id)['events']
                self.assertTrue(any(row['payload'].get('status') == 'redacted' for row in events))
                self.assertEqual(case.wire.errors, [])
                self.assertEqual(case.wire.responses, [])
                if case.s3 is not None:
                    self.assertEqual(case.s3.errors, [])
                    self.assertTrue(any(row['method'] == 'PUT' and row['body'] == SOURCE for row in case.s3.requests))
                    self.assertTrue(any(row['method'] == 'GET' and '/.storage-identities/' not in row['path']
                                        for row in case.s3.requests))
                composition.close()

    def test_disconnected_physical_request_keeps_unknown_budget_across_restart_without_resend(self):
        case = self.case('unknown-restart', responses=[None])
        composition = self.compose(case)
        scope, _ = self.publish(composition)
        session_id = composition.application.create(self.actor, scope)['session_id']
        request = {'message': 'What is the original rule?', 'request_id': 'interrupted-question',
                   'expected_revision': 0}
        result = composition.application.turn(self.actor, session_id, **request)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'PROVIDER_OUTCOME_UNKNOWN', result)
        self.assertEqual(result['answer'], '')
        self.assertEqual(result['claims'], [])
        self.assertEqual(len(case.wire.requests), 1)
        budget = composition.application.model_status()['budget']
        self.assertEqual(budget['calls'], 1)
        self.assertEqual(budget['unknown_usage_calls'], 1)
        self.assertGreater(budget['accounted_tokens'], 0)
        before = self.snapshot(composition)
        self.assertEqual(before['physical_requests'], 1)
        self.assertEqual(before['pending_or_unknown'], 1)
        self.assertEqual(before['effective_usage_or_reserved'], {'requests': 1, 'units': 10})
        records = {path.name: path.read_bytes() for path in (case.directory / 'audit').glob('*.json')}
        self.assertEqual(len(records), 1)
        record = json.loads(next(iter(records.values())))
        self.assertEqual(record['status'], 'UNKNOWN')
        self.assertTrue(record['physical_request_id'])
        composition.close()
        restored = self.compose(case)
        self.assertEqual(restored.application.turn(self.actor, session_id, **request), result)
        blocked = restored.application.turn(self.actor, session_id,
            message='Another question after the unresolved send', request_id='blocked-question', expected_revision=1)
        self.assertEqual(blocked['status'], 'error', blocked)
        self.assertEqual(blocked['stop_reason'], 'UNRESOLVED_PHYSICAL_REQUEST', blocked)
        self.assertEqual(len(case.wire.requests), 1)
        self.assertEqual(self.snapshot(restored), before)
        self.assertEqual({path.name: path.read_bytes() for path in (case.directory / 'audit').glob('*.json')}, records)
        self.assertEqual(case.wire.errors, [])

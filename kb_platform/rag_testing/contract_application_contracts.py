from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from kb_platform.application_knowledge import DEFAULT_APPLICATION, create_contract_knowledge
from kb_platform.model_adapters import LunaAdapter
from kb_platform.model_service import ModelService
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.settings import Settings
from kb_platform.storage_catalog import initialize_configuration

from .agent_graph_contracts import cited_answer, decision, verdict


class ContractApplicationContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.actor = Actor('synthetic-tenant', 'owner')
        self.token = 'synthetic-owner-token-with-32-characters'
        self.identities = self.root / 'identities.json'
        self.identities.write_text(json.dumps({self.token: {
            'tenant_id': self.actor.tenant_id, 'principal_id': self.actor.principal_id}}), 'utf-8')
        self.settings = Settings(self.root / 'store', self.identities, self.root / 'storage.json')
        self.script = [('decision', decision()), ('retrieval', decision('answer')),
                       ('answer', cited_answer), ('verifier', verdict())]
        self.physical = []
        self.cache = {}

        def post(path, payload, headers, purpose, key, *, trace):
            trace['attempt'] += 1
            role, value = self.script.pop(0)
            self.assertEqual(purpose, 'agent_' + role)
            self.physical.append(deepcopy(payload))
            value = value(payload['messages']) if callable(value) else value
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

        self.client = SimpleNamespace(provider_id='synthetic', model='controlled-model',
            base_url='https://synthetic.invalid', retries=0, manifest={'provider': 'synthetic'},
            _headers={}, _post=post, _cache_get=lambda key: self.cache.get(key),
            _cache_put=lambda key, value: self.cache.__setitem__(key, value),
            _record=Mock(), _save_failed_response=Mock(return_value={}))
        self.models = ModelService()
        self.models.register('generation', LunaAdapter(self.client))
        self.addCleanup(self.models.close)
        self.profile = json.loads(DEFAULT_APPLICATION.read_text('utf-8'))
        self.profile['plugins']['model']['config'].pop('providers_config')
        self.profile['plugins']['access']['config']['identities_file'] = str(self.identities)
        self.profile['plugins']['data']['config'].update(research_root=str(self.root / 'research'), rerank=False)
        self.profile['plugins']['workflow']['config']['retrieval_config'] = {
            'channels': ['bm25'], 'rerank': False, 'parent': 'none', 'title_navigation': False}
        storage = json.loads((Path(__file__).resolve().parents[2] /
                               'config/storage.local.example.json').read_text('utf-8'))
        for name, instance in storage['instances'].items():
            instance['config']['root'] = str(self.root / name)
        storage['bindings']['store'] = {'primary': {
            'service': 'records.domain', 'owner': 'sessions', 'space': 'history'}}
        self.storage = initialize_configuration(storage)

    def compose(self, **options):
        composition = create_contract_knowledge(self.settings, profile=self.profile,
            storage_configuration=self.storage, models=self.models, **options)
        self.addCleanup(composition.close)
        return composition

    def publish(self, composition):
        platform = composition.platform
        scope = platform.create_kb(self.actor, 'Synthetic protocol acceptance')['kb_id']
        source = platform.ingest(self.actor, scope, 'Original.md', b'Original rule value 42.',
            policy={'llm_context': True, 'embedding': True, 'allowed_providers': ['synthetic']})
        build = platform.stage(self.actor, scope, ['bm25'])
        platform.publish(self.actor, scope, build['build_id'])
        return scope, source

    def test_instance_names_are_selected_only_by_configuration(self):
        names = {name: 'selected-' + name for name in self.profile['plugins']}
        self.profile['plugins'] = {names[name]: value for name, value in self.profile['plugins'].items()}
        self.profile['bindings'] = {names[name]: {alias: {**target, 'plugin': names[target['plugin']]}
            for alias, target in bindings.items()} for name, bindings in self.profile['bindings'].items()}
        self.profile['entrypoint']['plugin'] = names[self.profile['entrypoint']['plugin']]
        composition = self.compose()
        self.assertEqual(composition.authenticate(self.token), self.actor)
        scope, _ = self.publish(composition)
        session = composition.application.create(self.actor, scope)
        self.assertEqual(composition.application.get(self.actor, session['session_id'])['scope_id'], scope)

    def test_default_application_accepts_explicit_borrowed_models(self):
        composition = create_contract_knowledge(self.settings, environment={},
            storage_configuration=self.storage, models=self.models)
        self.addCleanup(composition.close)
        self.assertEqual(composition.authenticate(self.token), self.actor)
        self.assertEqual(composition.application.model_status()['budget']['calls'], 0)
        self.assertEqual(len(composition.application.composition.plan['plugins']), 8)

    def test_actual_eight_plugin_graph_ingests_retrieves_answers_and_reuses_turn(self):
        composition = self.compose()
        self.assertEqual(len(composition.application.composition.plan['plugins']), 8)
        self.assertEqual(composition.authenticate(self.token), self.actor)
        with self.assertRaises(PlatformError) as caught:
            composition.authenticate('wrong-token')
        self.assertEqual(caught.exception.code, 'AUTHENTICATION_FAILED')
        with self.assertRaises(PlatformError) as caught:
            composition.authenticate('非ASCII' * 40)
        self.assertEqual(caught.exception.code, 'AUTHENTICATION_FAILED')
        scope, source = self.publish(composition)
        hits = composition.platform.search(self.actor, scope, 'Original rule')['hits']
        self.assertTrue(hits)
        citation = hits[0]['citation_id']
        self.assertEqual(composition.platform.read_original(self.actor, scope, citation),
                         b'Original rule value 42.')
        self.assertEqual(source['document']['status'], 'READY')
        application = composition.application
        session = application.create(self.actor, scope)
        request = dict(message='What is the original rule?', request_id='durable-request', expected_revision=0,
                       constraints=['Use only the original rule'])
        result = application.turn(self.actor, session['session_id'], **request)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['answer'], 'Original rule value 42.')
        self.assertEqual(len(self.physical), 4)
        self.assertEqual(application.turn(self.actor, session['session_id'], **request), result)
        self.assertEqual(len(self.physical), 4)
        self.assertEqual(application.model_status()['budget']['calls'], 4)
        self.assertEqual(application.get(self.actor, session['session_id'])['revision'], 1)
        self.assertTrue(application.history(self.actor, session['session_id'])['events'])
        with self.assertRaises(PlatformError) as caught:
            application.get(Actor(self.actor.tenant_id, 'other'), session['session_id'])
        self.assertEqual(caught.exception.code, 'SESSION_NOT_FOUND')
        with self.assertRaises(PlatformError):
            composition.platform.search(Actor('other-tenant', 'owner'), scope, 'Original')

    def test_context_session_and_model_implementations_replace_by_configuration(self):
        from dataclasses import asdict

        from kb_platform.agent_context import ContextManager, ContextPolicy
        from kb_platform.agent_luna import LunaModelProvider
        from kb_platform.agent_model import AgentModel, BudgetLedger
        from kb_platform.application_storage import StorageClient
        from kb_platform.knowledge_capabilities import CATALOG, AuthorizedEvidenceClient
        from kb_platform.knowledge_context import KnowledgeContext, LocalEvidenceSource, LocalHistorySource
        from kb_platform.knowledge_contracts import ModelEndpoint, SessionClient, SessionEndpoint
        from kb_platform.knowledge_model_ports import AuxiliaryModelEndpoint
        from kb_platform.knowledge_sessions import KnowledgeSessions
        from kb_platform.session_store import SessionStore

        selected, mounted = [], []
        for name in ('context', 'sessions', 'model'):
            replacement = deepcopy(next(item for item in CATALOG if item['implementation'] == 'knowledge.' + name))
            replacement['implementation'] = 'synthetic.replacement.' + name
            selected.append(replacement)
            self.profile['plugins'][name]['implementation'] = replacement['implementation']

        def sessions(ctx, config):
            store = SessionStore(database=StorageClient(ctx.require('storage'), config['binding']).relational())
            ctx.defer(store.close)
            ctx.provide('sessions', SessionEndpoint(KnowledgeSessions(store)))
            mounted.append('sessions')

        def model(ctx, config):
            selected = AgentModel(LunaModelProvider(self.client),
                policy=ContextPolicy(**config['policy']), ledger=BudgetLedger(max_calls=7))
            selected.provider_id, selected.manifest = 'synthetic', {'provider': 'synthetic'}
            endpoint, auxiliary = ModelEndpoint(selected), AuxiliaryModelEndpoint()
            ctx.defer(endpoint.close)
            ctx.defer(auxiliary.close)
            ctx.provide('model', endpoint)
            ctx.provide('capabilities', auxiliary)
            mounted.append('model')

        def context(ctx, config):
            policy = ContextPolicy(**config)
            selected = KnowledgeContext(LocalHistorySource(SessionClient(ctx.require('sessions'))),
                LocalEvidenceSource(AuthorizedEvidenceClient(ctx.require('data'), ctx.require('access'))),
                assembler=ContextManager(policy))

            class ReplacementContext:
                def sources(self, request):
                    return selected.sources(request)

                def metadata(self, request):
                    return {'policy': asdict(policy)}

                def prepare_request(self, request):
                    return selected.prepare_request({**request,
                        'system': request['system'] + '\nSYNTHETIC_REPLACEMENT_CONTEXT'})
            ctx.provide('context', ReplacementContext())
            mounted.append('context')

        composition = self.compose(extra_catalog=selected, extra_mounts={
            ('synthetic.replacement.sessions', '1'): sessions,
            ('synthetic.replacement.model', '1'): model,
            ('synthetic.replacement.context', '1'): context}, schemas={
                (item['implementation'], '1'): {'type': 'object'} for item in selected})
        self.assertEqual(set(mounted), {'sessions', 'model', 'context'})
        scope, _ = self.publish(composition)
        application = composition.application
        session = application.create(self.actor, scope)
        result = application.turn(self.actor, session['session_id'], message='What is the original rule?',
                                  request_id='replacement-request', expected_revision=0)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(application.model_status()['budget']['max_calls'], 7)
        self.assertEqual(len(self.physical), 4)
        self.assertTrue(all('SYNTHETIC_REPLACEMENT_CONTEXT' in request['messages'][0]['content']
                            for request in self.physical))

    def test_embedding_authority_rechecks_source_revocation_and_build_identity(self):
        from hashlib import sha256

        from kb_platform.plugin_contracts import ContractError

        composition = self.compose()
        scope, source = self.publish(composition)
        authority = composition.application.authority(self.actor, scope)
        audit = {'tenant_id': self.actor.tenant_id, 'principal': self.actor.principal_id,
            'kb_id': scope, 'build_id': authority['manifest_id'], 'acl_revision': str(authority['authority_revision']),
            'sources': [{'source_id': source['source_id'], 'source_version_id': source['source_version_id'],
                'parse_snapshot_id': source['document']['parse_snapshot_id'], 'source_text_sha256': sha256(
                    '\n'.join(row['raw_text'] for row in source['document']['elements']).encode()).hexdigest()}]}
        request = {'audit_context': audit, 'task': 'query', 'provider_id': 'synthetic'}
        access = composition.application.composition
        self.assertEqual(access.call('access', 'access', 'validate_embedding', request), {'authorized': True})
        with self.assertRaises(ContractError) as caught:
            access.call('access', 'access', 'validate_embedding', {**request, 'task': 'document'})
        self.assertEqual(caught.exception.code, 'STALE_BUILD')
        for invalid in ({**audit, 'build_id': 'not-current'},
                        {**audit, 'sources': [{**audit['sources'][0], 'source_text_sha256': 'changed'}]}):
            with self.assertRaises(ContractError) as caught:
                access.call('access', 'access', 'validate_embedding', {**request, 'audit_context': invalid})
            self.assertEqual(caught.exception.code, 'STALE_BUILD')
        composition.platform.set_source_policy(self.actor, source['source_id'], {
            'llm_context': True, 'embedding': False, 'allowed_providers': ['synthetic']})
        with self.assertRaises(ContractError) as caught:
            access.call('access', 'access', 'validate_embedding', request)
        self.assertEqual(caught.exception.code, 'STALE_BUILD')
        current = composition.application.authority(self.actor, scope)
        audit['acl_revision'] = str(current['authority_revision'])
        with self.assertRaises(ContractError) as caught:
            access.call('access', 'access', 'validate_embedding', request)
        self.assertEqual(caught.exception.code, 'PROCESSING_NOT_ALLOWED')
        self.assertEqual(self.physical, [])

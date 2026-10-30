from copy import deepcopy
from dataclasses import replace
import json
from threading import Lock
from unittest import TestCase
from unittest.mock import patch

import httpx

from kb_platform.agent_model import AgentModel
from kb_platform.agent_plugins import ToolPlugin
from kb_platform.knowledge_plugin_tools import KnowledgeToolPlugins as ToolPlugins, KnowledgeToolResult as ToolResult
from kb_platform.agent_provider import ProviderCompletion
from kb_platform.agent_runtime import send_deadline, serialized_send
from kb_platform.agent_call_budget import CallBudget
from kb_platform.agent_tool_view import project_tool_results
from kb_platform.knowledge_service import KnowledgeService as AgentService
from kb_platform.core import PlatformError
from kb_platform.model_errors import RemoteModelError
from kb_platform.provider_runtime import RuntimePolicyError
from . import agent_graph_contracts as fixtures


class SyntheticProvider:
    max_attempts = 1

    def __init__(self):
        self.calls, self.cache, self.records = [], {}, []

    def identity(self, messages, output_limit):
        return {'provider': 'synthetic/1', 'messages': messages, 'output_limit': output_limit}

    def complete(self, messages, output_limit, *, purpose, key, trace):
        self.calls.append((messages, output_limit))
        trace['attempt'] += 1
        return ProviderCompletion('{"answer":"ok"}', 'stop',
                                  {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})

    def cache_get(self, key):
        return deepcopy(self.cache.get(key))

    def cache_put(self, key, value):
        self.cache[key] = deepcopy(value)

    def record(self, purpose, key, **metadata):
        self.records.append(metadata)

    def save_failed_response(self, result, metadata):
        return {'diagnostic_id': 'synthetic'}


class ProviderPortContracts(TestCase):
    def test_provider_without_luna_or_http_payload_supports_current_validation_and_cache(self):
        provider = SyntheticProvider()
        model = AgentModel(provider)
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}
        kwargs = {'role': 'answer', 'schema': schema, 'call_context': {}}
        first = model.complete([{'role': 'user', 'content': 'synthetic'}], **kwargs)
        second = model.complete([{'role': 'user', 'content': 'synthetic'}], **kwargs)
        self.assertEqual(first.value, {'answer': 'ok'})
        self.assertEqual((first.attempts, second.attempts), (1, 0))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(model.ledger.snapshot()['calls'], 1)
        with self.assertRaises(RemoteModelError) as failure:
            def reject(value):
                raise ValueError('CURRENT_VALIDATOR_REJECTS')
            model.complete([{'role': 'user', 'content': 'synthetic'}], **kwargs, validator=reject)
        self.assertEqual(failure.exception.code, 'AGENT_CACHE_CONTRACT_INVALID')


class ToolPluginContracts(TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

    def plugin(self, execute=None):
        return ToolPlugin('lookup_rule', 'Read a synthetic authorized rule.',
                          {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 1}},
                           'required': ['query'], 'additionalProperties': False},
                          execute or (lambda context, args: ToolResult({'value': 42})))

    def install(self, case, plugins):
        case.service = AgentService(None, case.model, root=case.root, gateway=case.gateway,
                                    tool_plugins=plugins)

    def test_plugin_is_planned_executed_validated_persisted_and_delivered(self):
        case = self.setup_service([
            ('decision', fixtures.decision('lookup_rule', plugin_args={'query': 'original'})),
            ('retrieval', fixtures.decision('answer')), ('answer', fixtures.cited_answer),
            ('verifier', fixtures.verdict())])
        calls = []

        def execute(context, args):
            calls.append((context.scope_id, args))
            hits = case.gateway.search(context.actor, context.scope_id, args['query'], context.source_policy)
            return ToolResult({'value': 42}, hits)

        self.install(case, [self.plugin(execute)])
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['answer'], 'Original rule: value 42.')
        self.assertIn('lookup_rule', case.service.capabilities()['tools'])
        events = case.service.store.events(case.authority.actor, case.session)
        receipt = next(e['payload'] for e in events if e['kind'] == 'tool_result')
        artifact = case.service.artifact(case.authority.actor, case.session, receipt['artifact_id'])
        self.assertIn('42', json.dumps(artifact))

    def test_registry_is_per_service_and_rejects_duplicate_or_retrying_side_effects(self):
        plugin = self.plugin()
        registry = ToolPlugins([plugin])
        plugin.parameters['required'] = []
        with self.assertRaises(ValueError):
            registry.validate_arguments(plugin.name, {})
        self.assertNotIn(plugin.name, ToolPlugins())
        for plugins in ([plugin, plugin], [replace(plugin, name='search')],
                        [replace(plugin, retry_safe=True, incurs_cost=True)],
                        [replace(plugin, retry_safe=True, mutates_data=True)]):
            with self.subTest(plugins=plugins), self.assertRaises(ValueError):
                ToolPlugins(plugins)

    def test_plugin_cannot_deliver_revoked_evidence(self):
        case = self.setup_service([
            ('decision', fixtures.decision('lookup_rule', plugin_args={'query': 'original'}))])

        def execute(context, args):
            hits = case.gateway.search(context.actor, context.scope_id, args['query'], context.source_policy)
            hits[0]['text'] = 'forged source'
            return ToolResult({}, hits)

        self.install(case, [self.plugin(execute)])
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertNotIn('forged source', result['answer'])
        self.assertEqual(result['claims'], [])


class RuntimeLifecycleContracts(TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

    def test_planning_with_reranking_preserves_answer_and_verification_calls(self):
        def ranking(messages):
            projection = json.loads(messages[-1]['content'])
            return {'scores': [index / len(projection['evidence'])
                               for index, _ in enumerate(projection['evidence'])]}

        def search_pipeline(gateway, actor, scope, query, policy, config, **kwargs):
            hits = gateway.search(actor, scope, query, policy)
            if config.get('rerank'):
                ranked = gateway.pipeline_reranker.rank(query, hits)
                self.assertEqual(ranked, [{'citation_id': item['citation_id'], 'score': index / len(hits)}
                                         for index, item in enumerate(hits)])
            return {'hits': hits, 'trace': {'expansion': []}}

        for limit in (5, 6):
            script = [('decision', fixtures.decision('search_pipeline', query='first')), ('rerank', ranking),
                      ('retrieval', fixtures.decision('search_pipeline', query='second'))]
            if limit == 6:
                script.append(('rerank', ranking))
            case = self.setup_service(script + [('answer', fixtures.cited_answer), ('verifier', fixtures.verdict())],
                                      config={'max_model_calls': limit})
            case.service.retrieval_config = {'channels': ['bm25'], 'rerank': True}
            with patch.object(type(case.gateway), 'search_pipeline', search_pipeline), \
                    patch.object(type(case.gateway), 'pipeline_capabilities', return_value={
                        'channels': ['bm25'], 'rerank_all_sources_admitted': True}):
                result = self.turn(case)
            self.assertEqual(result['status'], 'partial', result)
            self.assertEqual(result['stop_reason'], 'exploration_budget')
            self.assertEqual(len(case.model.calls), limit)
            self.assertEqual([c['role'] for c in case.model.calls[-2:]], ['answer', 'verifier'])
            self.assertEqual(len(result['trace']['tool_calls']), 2)

    def test_exploration_reserves_worst_case_physical_attempts(self):
        policy = CallBudget(10, True, max_attempts=3)
        self.assertTrue(policy.can_explore([]))
        self.assertFalse(policy.can_explore([{'attempts': 3}]))
        self.assertTrue(policy.can_explore([{'attempts': 0}]))

    def test_large_tool_view_is_bounded_with_full_result_preserved(self):
        original = {'name': 'table', 'status': 'ok', 'args': {'operation': 'rows'},
                    'artifact_id': 'a' * 64,
                    'result': {'rows': [['value' * 100] for _ in range(1000)], 'count': 1000}}
        before = deepcopy(original)
        view = project_tool_results([original], budget_bytes=1000)[0]
        self.assertLess(len(json.dumps(view).encode('utf-8')), 1000)
        self.assertEqual(view['artifact_id'], original['artifact_id'])
        self.assertEqual(view['result']['count'], 1000)
        self.assertFalse(view['result']['complete_result'])
        self.assertEqual(original, before)

    def test_expiry_while_waiting_never_sends_and_releases_acquired_lock(self):
        lock = Lock()
        sent = []
        with send_deadline(5), patch('kb_platform.agent_runtime.time.monotonic', side_effect=[4, 6]):
            with self.assertRaises(RuntimePolicyError) as failure:
                serialized_send(lambda request: sent.append(request), lock, httpx.Request('GET', 'https://invalid'))
        self.assertEqual(failure.exception.code, 'PROVIDER_DEADLINE_EXCEEDED')
        self.assertEqual(sent, [])
        self.assertFalse(lock.locked())

    def test_queue_acquisition_is_bounded_and_http_timeout_recomputed(self):
        class Gate:
            def acquire(self, timeout):
                self.timeout = timeout
                return False
        gate = Gate()
        with send_deadline(5), patch('kb_platform.agent_runtime.time.monotonic', return_value=3):
            with self.assertRaises(RuntimePolicyError):
                serialized_send(lambda: self.fail('must not send'), gate)
        self.assertEqual(gate.timeout, 2)
        request = httpx.Request('GET', 'https://invalid', extensions={'timeout': {'read': 20, 'connect': None}})
        with send_deadline(5), patch('kb_platform.agent_runtime.time.monotonic', side_effect=[1, 3, 3]):
            serialized_send(lambda request: None, Lock(), request)
        self.assertEqual(request.extensions['timeout'], {'read': 2, 'connect': 2})

    def test_expired_writer_can_atomically_close_failure_but_not_overwrite_successor(self):
        case = self.setup_service()
        store, actor = case.service.store, case.authority.actor
        response = {'status': 'error', 'answer': '', 'claims': [], 'evidence': [], 'stop_reason': 'TIMEOUT'}
        with patch('kb_platform.session_store.time.time', return_value=1):
            first = store.begin(actor, case.session, 'first', 0, 'fingerprint', lease_seconds=1)
        with patch('kb_platform.session_store.time.time', return_value=3):
            closed = store.fail(actor, case.session, first['token'], first['session']['state'], response)
            self.assertEqual(closed['revision'], 1)
            cached = store.begin(actor, case.session, 'first', 0, 'fingerprint')
            self.assertEqual(cached['cached'], closed)
            second = store.begin(actor, case.session, 'second', 1, 'two', lease_seconds=1)
        with patch('kb_platform.session_store.time.time', return_value=5):
            store.begin(actor, case.session, 'third', 1, 'three')
            with self.assertRaises(PlatformError):
                store.fail(actor, case.session, second['token'], second['session']['state'], response)
        self.assertEqual(store.load(actor, case.session)['revision'], 1)

    def test_service_timeout_and_lease_expire_together_and_failure_is_cached(self):
        clock = {'wall': 1, 'elapsed': 0}

        def after(role, value):
            clock.update(wall=1000, elapsed=1000)

        case = self.setup_service([('decision', fixtures.decision())], after=after)
        with patch('kb_platform.session_store.time.time', side_effect=lambda: clock['wall']), \
                patch('kb_platform.knowledge_turn.time.perf_counter', side_effect=lambda: clock['elapsed']):
            result = self.turn(case)
            self.assertEqual(result['status'], 'error', result)
            self.assertEqual(self.turn(case), result)
        self.assertEqual(len(case.model.calls), 1)

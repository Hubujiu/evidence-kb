from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.core import Actor, PlatformError
from kb_platform.knowledge_policy import CONFIGS, RECOMMENDED
from kb_platform.knowledge_profile import create_knowledge_application, load_knowledge_profile
from kb_platform.knowledge_service import KnowledgeService
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_host import PluginSpec
from . import agent_graph_contracts as graph


class KnowledgeLifecycleContracts(unittest.TestCase):
    def fixture(self, script=None):
        case = graph.AgentGraphContracts.setup_service(self, graph.normal_script() if script is None else script)
        profile = load_knowledge_profile(environment={})
        next(row for row in profile['plugins'] if row['id'] == 'store')['config']['root'] = str(case.root)
        case.profile = profile
        return case

    def application(self, case, model=None, extra_plugins=()):
        def factory(platform, configured_model, **settings):
            return KnowledgeService(platform, case.model if model is None else model,
                **{**settings, 'gateway': case.gateway, 'retrieval_config': {'channels': ['bm25'], 'rerank': False}})

        application, host = create_knowledge_application(case.authority, SimpleNamespace(luna=SimpleNamespace(provider_id='magpie_luna', manifest={})),
            profile=case.profile, service_factory=factory, extra_plugins=extra_plugins, environment={})
        self.addCleanup(host.close)
        return application, host

    @staticmethod
    def turn(application, case, actor=None):
        return application.turn(case.authority.actor if actor is None else actor, case.session,
            message='What is the original rule?', request_id='turn', expected_revision=0)

    def test_completed_turn_survives_host_restart_without_duplicate_model_or_receipts(self):
        case = self.fixture()
        application, host = self.application(case)
        result = self.turn(application, case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['answer'], 'Original rule: value 42.')
        self.assertEqual(len(case.model.calls), 4)
        stored = deepcopy(application.history(case.authority.actor, case.session))
        host.close()
        restarted_model = graph.ScenarioModel([])
        restarted, _ = self.application(case, restarted_model)
        self.assertEqual(self.turn(restarted, case), result)
        self.assertEqual(restarted_model.calls, [])
        self.assertEqual(restarted.history(case.authority.actor, case.session), stored)
        self.assertEqual(restarted.get(case.authority.actor, case.session)['revision'], 1)

    def test_product_selects_graph_factory_without_outer_model_loop(self):
        from kb_platform.harness_graph import native_call
        from kb_platform.knowledge_workflow import KnowledgeWorkflow

        created = []

        def factory(**options):
            created.append(options['step_limit'])
            return KnowledgeWorkflow(**options)

        case = self.fixture()
        row = next(row for row in case.profile['plugins'] if row['id'] == 'workflow')
        row['use'] = 'fixture.workflow'
        plugin = PluginSpec('fixture.workflow', ('knowledge.workflow',), (),
                            lambda ctx, config: ctx.provide('knowledge.workflow', factory))
        application, _ = self.application(case, extra_plugins=(plugin,))
        with patch('kb_platform.harness_graph.native_call', wraps=native_call) as graph_calls, \
                patch('kb_platform.harness_loop.native_call', side_effect=AssertionError('outer loop used')):
            result = self.turn(application, case)
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(len(created), 1)
        self.assertEqual(len(case.model.calls), 4)
        self.assertIn('graph_create', [call.args[0] for call in graph_calls.call_args_list])
        self.assertEqual(graph_calls.call_args_list[-1].args[0], 'graph_close')

    def test_hosted_cached_response_rechecks_owner_and_current_source_permission(self):
        case = self.fixture()
        application, host = self.application(case)
        result = self.turn(application, case)
        self.assertEqual(result['status'], 'answered', result)
        with self.assertRaises(PlatformError) as denied:
            self.turn(application, case, Actor('tenant', 'intruder'))
        self.assertEqual(denied.exception.code, 'SESSION_NOT_FOUND')
        case.authority.rows[0]['policy']['llm_context'] = False
        with self.assertRaises(PlatformError):
            self.turn(application, case)
        self.assertEqual(len(case.model.calls), 4)
        history = application.history(case.authority.actor, case.session)
        completed = [event for event in history['events'] if event['kind'] == 'turn_completed']
        self.assertEqual(completed[0]['payload'], {'status': 'redacted', 'reason': 'source_unavailable'})
        self.assertEqual(application.get(case.authority.actor, case.session)['state']['evidence'], {})
        self.assertEqual(host.service('knowledge').store.load(case.authority.actor, case.session)['revision'], 1)

    def test_budget_failure_is_durable_and_not_retried_by_outer_native_loop(self):
        case = self.fixture()
        application, host = self.application(case)
        with patch.dict(CONFIGS, {RECOMMENDED: {**CONFIGS[RECOMMENDED], 'max_model_calls': 2}}):
            failed = self.turn(application, case)
            self.assertEqual(failed['status'], 'error', failed)
            self.assertEqual(failed['stop_reason'], 'AGENT_BUDGET_EXHAUSTED')
            self.assertEqual(self.turn(application, case), failed)
        self.assertEqual(case.model.calls, [])
        events = host.service('knowledge').store.events(case.authority.actor, case.session)
        self.assertEqual(len([event for event in events if event['kind'] == 'turn_completed']), 1)

    def test_closed_host_rejects_application_calls_before_model_or_session_mutation(self):
        case = self.fixture()
        application, host = self.application(case)
        host.close()
        for call in (application.capabilities,
                     lambda: application.get(case.authority.actor, case.session),
                     lambda: self.turn(application, case)):
            with self.subTest(call=call), self.assertRaises(PluginError):
                call()
        self.assertEqual(case.model.calls, [])
        self.assertEqual(case.service.store.events(case.authority.actor, case.session), [])
        self.assertEqual(case.service.store.load(case.authority.actor, case.session)['revision'], 0)

    def test_profile_closes_owned_provider_after_later_mount_failure_but_not_borrowed_provider(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / 'providers.json'
            config_path.write_text(json.dumps({'luna': {'base_url': 'https://fixture.invalid/v1',
                'model': 'fixture', 'api_key': 'synthetic'}}), 'utf-8')
            profile = load_knowledge_profile(environment={})
            next(row for row in profile['plugins'] if row['id'] == 'store')['config']['root'] = str(Path(directory))
            provider = next(row for row in profile['plugins'] if row['id'] == 'provider')
            provider['config'] = {'providers_config': str(config_path)}
            borrowed, owned = Mock(), Mock()

            def fail_after_mount(*args, **kwargs):
                raise RuntimeError('synthetic knowledge mount failure')

            with patch('kb_platform.remote.LunaClient', return_value=owned) as constructor:
                with self.assertRaisesRegex(RuntimeError, 'synthetic knowledge mount failure'):
                    create_knowledge_application(object(), SimpleNamespace(luna=borrowed), profile=profile,
                        service_factory=fail_after_mount, environment={})
            constructor.assert_called_once_with(str(config_path))
            owned.close.assert_called_once_with()
            borrowed.close.assert_not_called()
            provider['config'] = {}
            with self.assertRaisesRegex(RuntimeError, 'synthetic knowledge mount failure'):
                create_knowledge_application(object(), SimpleNamespace(luna=borrowed), profile=profile,
                    service_factory=fail_after_mount, environment={})
            borrowed.close.assert_not_called()

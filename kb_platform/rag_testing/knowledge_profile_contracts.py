"""Synthetic profile/HTTP contracts; no provider calls or RAG quality claims."""
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.core import Actor
from kb_platform.knowledge_profile import create_knowledge_application, load_knowledge_profile
from kb_platform.plugin_host import PluginSpec


class _KnowledgeFixture:
    def __init__(self, platform, model, *, research, root, store, context_policy, context_manager, runtime,
                 gateway, tool_executor, semantic_pages):
        self.model, self.store, self.runtime = model, store, runtime
        self.semantic_pages, self.tool_context = None, None
        self.calls = []

    def turn(self, actor, session_id, **request):
        self.calls.append(deepcopy(request))
        return {'session_id': session_id, 'status': 'complete', 'answer': 'Verified fixture answer.',
                'revision': request['expected_revision'] + 1, 'request': request,
                'trace': {'synthetic': True, 'provider_calls': 0}, 'principal': actor.principal_id}

    def capabilities(self):
        return {'tools': ['fixture'], 'calls': len(self.calls)}


class KnowledgeProfileContracts(unittest.TestCase):
    def profile(self, root):
        profile = load_knowledge_profile(environment={})
        next(row for row in profile['plugins'] if row['id'] == 'store')['config']['root'] = str(root)
        return profile

    def application(self, root, *, profile=None, plugins=()):
        application, host = create_knowledge_application(
            object(), SimpleNamespace(luna=object()), profile=profile or self.profile(root),
            service_factory=_KnowledgeFixture, extra_plugins=plugins, environment={})
        self.addCleanup(host.close)
        return application, host

    def request(self):
        return dict(message='What applies?', request_id='http-original-123', expected_revision=7,
                    policy={'source_ids': ['source-a']}, constraints=['Use current sources'],
                    workflow_state={'current_step': 'review', 'facts': {'approved': False}})

    def test_environment_overlay_is_explicit_and_does_not_mutate_profile(self):
        original = load_knowledge_profile(environment={})
        before = deepcopy(original)
        selected = load_knowledge_profile(original, environment={'KB_AGENT_INPUT_BUDGET': '8000',
                                                                  'KB_AGENT_MAX_CALLS': '3'})
        self.assertEqual(original, before)
        by_id = {row['id']: row for row in selected['plugins']}
        self.assertEqual(by_id['policy']['config']['input_budget'], 8000)
        self.assertEqual(by_id['budget']['config']['max_calls'], 3)
        self.assertEqual(by_id['loop']['config']['mode'], 'single-tool')

    def test_default_loop_preserves_http_identity_controls_and_verified_trace(self):
        with TemporaryDirectory() as directory:
            application, host = self.application(directory)
            request = self.request()
            answer = application.turn(Actor('tenant', 'owner'), 'session-1', **request)
            self.assertEqual(answer['request'], request)
            self.assertEqual(answer['revision'], 8)
            self.assertEqual(answer['answer'], 'Verified fixture answer.')
            self.assertEqual(answer['trace'], {'synthetic': True, 'provider_calls': 0})
            self.assertEqual(application.capabilities()['calls'], 1)
            self.assertEqual(application.model.ledger.snapshot()['calls'], 0)
            host.close()

    def test_profile_can_replace_loop_without_changing_application_or_domain(self):
        class ReplacementLoop:
            def invoke(self, tool, context, arguments):
                self.asserted = (tool.name, context.session_id, arguments)
                return {**tool.execute(context, arguments).data, 'selected_loop': 'replacement'}

        loop = ReplacementLoop()
        replacement = PluginSpec('fixture.loop', ('loop',), (), lambda ctx, config: ctx.provide('loop', loop))
        with TemporaryDirectory() as directory:
            profile = self.profile(directory)
            selected = next(row for row in profile['plugins'] if row['id'] == 'loop')
            selected.update(use='fixture.loop', config={})
            application, host = self.application(directory, profile=profile, plugins=(replacement,))
            answer = application.turn(Actor('tenant', 'owner'), 'session-1', **self.request())
            self.assertEqual(answer['selected_loop'], 'replacement')
            self.assertEqual(loop.asserted, ('kb_query', 'session-1', {'question': 'What applies?'}))
            self.assertEqual(application.capabilities()['calls'], 1)
            host.close()

    def test_default_route_wiring_uses_profile_and_original_request_contract(self):
        from fastapi import FastAPI
        from kb_platform.agent_api import TurnRequest, install_agent_routes

        with TemporaryDirectory() as directory:
            environment = {key: value for key, value in os.environ.items() if not key.startswith('KB_AGENT_')}
            environment['KB_AGENT_ROOT'] = str(Path(directory) / 'sessions')
            with patch.dict(os.environ, environment, clear=True):
                app = FastAPI()
                install_agent_routes(app, object(), SimpleNamespace(luna=object()),
                                     lambda: Actor('tenant', 'owner'), service_factory=_KnowledgeFixture)
                request = self.request()
                body = {**request, 'source_policy': request['policy']}
                del body['policy']
                routes = {route.path: route.endpoint for route in app.routes}
                with app.state.agent_plugins:
                    result = routes['/agent/sessions/{session_id}/turns'](
                        session_id='session-1', body=TurnRequest.model_validate(body),
                        actor=Actor('tenant', 'owner'))
                    self.assertEqual(result['request'], request)
                    self.assertEqual(routes['/agent/capabilities'](Actor('tenant', 'owner'))['calls'], 1)
                    self.assertIsNotNone(app.state.agent_plugins)
                    self.assertEqual(app.state.agent.model.ledger.snapshot()['calls'], 0)

    def test_service_replay_keeps_final_request_binding_under_plugin_loop(self):
        from .contracts import AdapterInput, Case, Evidence, Plan, Source, Suite, digest, text_hash
        from .default_path_replay import ProductAdapter
        from .scoring import corpus_hash, score_case

        text = 'Harbor Guide is valid only until 2030.'
        source = Source(id='guide', kb='scope', version='fixed-v1', text=text, sha256=text_hash(text))
        request = AdapterInput(case_id='plugin-replay', question='What does Harbor Guide require?',
                               kb='scope', principal='reader', acl_revision='1', allowed_sources=('guide',),
                               as_of='2026-10-29', context_budget=24000)
        evidence = Evidence(id='rule', source_id=source.id, start=0, end=len(text), quote=text,
                            fact='The guide expires in 2030.', qualifiers=('only until 2030',))
        case = Case(input=request, family='synthetic-plugin-replay', any_of=(('rule',),))
        suite = Suite(id='synthetic-plugin-replay/1', label_provenance='synthetic_engineering',
                      sources=(source,), evidence=(evidence,), cases=(case,))
        adapter = ProductAdapter([{'id': source.id, 'title': 'Harbor Guide', 'text': text}],
                                 enabled=True, service=True)
        trace = adapter.run(request, (source,), corpus_hash(suite))
        plan = Plan(suite_hash=digest(suite), objective='evidence',
                    required_stages=('retrieval', 'assembly'))
        score = score_case(suite, case, trace, plan)
        self.assertEqual(score['outcome'], 'PASS', score)
        self.assertEqual(trace.capture, 'pre_send')
        self.assertTrue(trace.request_evidence_bindings)
        self.assertEqual(adapter.observations[request.case_id]['new_remote_calls'], 0)


if __name__ == '__main__':
    unittest.main()

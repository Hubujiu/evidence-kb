import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

import httpx

from kb_platform.agent_engine import execution_scope
from kb_platform.model_adapters import EmbeddingAdapter, LunaAdapter
from kb_platform.model_configuration import EmbeddingView, OCRView, from_config
from kb_platform.model_http import AnthropicAdapter, OpenAICompatibleAdapter
from kb_platform.model_plugins import model_plugins
from kb_platform.model_service import Capability as C, Media, ModelInfo, ModelRequest, ModelResult, ModelService
from kb_platform.plugin_host import PluginHost, PluginSpec
from .budget import BudgetLedger


class ModelServiceContracts(unittest.TestCase):
    def configuration(self, routes):
        return {'version': 1, 'budget': {'path': str(self.root / 'api-budget.sqlite3'), 'caps': {'requests': 30}},
            'providers': {'fixture': {'adapter': 'openai-compatible', 'base_url': 'https://fixture.invalid/v1',
                                       'api_key_env': 'SYNTH_KEY'}},
            'models': {route: {'provider': 'fixture', 'model': 'fixture', 'reservation': {'requests': 1},
                'capabilities': ['chat' if route == 'generation' else route],
                **({'dimension': 2} if route == 'embedding' else {})} for route in routes}}

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ledger = BudgetLedger(self.root / 'calls.sqlite3', {'requests': 30, 'units': 300})
        self.sent = []

    def adapter(self, reply, *, provider='vendor-a', model='model-a', capabilities=tuple(C), cls=OpenAICompatibleAdapter):
        def handle(request):
            self.sent.append(request)
            return reply(request) if callable(reply) else httpx.Response(200, json=reply)
        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return cls(provider=provider, model=model, endpoint='https://synthetic.invalid/v1', api_key='synthetic',
                   capabilities=capabilities, ledger=self.ledger, reservation={'requests': 1, 'units': 10}, client=client)

    def test_actual_wire_formats_and_normalized_results_for_all_six_capabilities(self):
        png = b'\x89PNG\r\n\x1a\nsynthetic'
        def reply(request):
            path = request.url.path
            if path.endswith('/audio/speech'):
                return httpx.Response(200, content=b'synthetic mp3', headers={'content-type': 'audio/mpeg'})
            if path.endswith('/audio/transcriptions'):
                self.assertIn('multipart/form-data', request.headers['content-type'])
                self.assertIn(b'audio.wav', request.content)
                return httpx.Response(200, json={'text': 'spoken words'})
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'model-a')
            if path.endswith('/embeddings'):
                return httpx.Response(200, json={'data': [{'index': 1, 'embedding': [3., 4.]},
                                                        {'index': 0, 'embedding': [1., 2.]}]})
            if path.endswith('/images/generations'):
                return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png).decode()}]})
            content = payload['messages'][-1]['content']
            text = '{"text":"visible words"}' if isinstance(content, list) else 'answer'
            return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})
        service = ModelService()
        service.register('selected', self.adapter(reply))
        requests = [
            ModelRequest(C.CHAT, {'messages': [{'role': 'user', 'content': 'question'}]}),
            ModelRequest(C.EMBEDDING, {'texts': ['one', 'two'], 'dimensions': 2}),
            ModelRequest(C.OCR, {'media': Media(png, 'image/png')}),
            ModelRequest(C.TRANSCRIPTION, {'media': Media(b'RIFF synthetic', 'audio/wav')}),
            ModelRequest(C.SPEECH, {'text': 'hello'}), ModelRequest(C.IMAGE, {'prompt': 'a tree'}),
        ]
        results = [service.invoke('selected', req) for req in requests]
        self.assertEqual(results[0].data['text'], 'answer')
        self.assertEqual(results[1].data['vectors'], [[1., 2.], [3., 4.]])
        self.assertEqual(results[2].data['text'], 'visible words')
        self.assertEqual(results[3].data['text'], 'spoken words')
        self.assertEqual(results[4].data['media'].mime_type, 'audio/mpeg')
        self.assertEqual(results[5].data['images'][0]['media'].data, png)
        self.assertEqual(len({result.request_id for result in results}), 6)
        self.assertTrue(all(result.usage is None for result in results))
        self.assertEqual(self.ledger.summary()['physical_requests'], 6)
        self.assertEqual(self.ledger.summary()['effective_usage_or_reserved'], {'requests': 6, 'units': 60})

    def test_same_consumer_supports_openai_and_native_anthropic_and_shared_ledger(self):
        service = ModelService()
        service.register('a', self.adapter({'choices': [{'message': {'content': '{"answer":"A"}'},
                                                       'finish_reason': 'stop'}]}))
        service.register('b', self.adapter({'content': [{'type': 'text', 'text': '{"answer":"B"}'}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 4, 'output_tokens': 3}},
            provider='vendor-b', model='model-b', capabilities=[C.CHAT], cls=AnthropicAdapter))
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}
        for route, expected in [('a', 'A'), ('b', 'B')]:
            model = service.structured(route)
            result = model.complete([{'role': 'user', 'content': 'same consumer'}], role='answer',
                                    schema=schema, call_context={})
            self.assertEqual(result.value, {'answer': expected})
            self.assertEqual(model.ledger.snapshot()['calls'], 1)
        self.assertEqual(self.sent[1].url.path, '/v1/messages')
        self.assertIn('x-api-key', self.sent[1].headers)
        self.assertNotIn('authorization', self.sent[1].headers)
        self.assertIn('system', json.loads(self.sent[1].content))
        self.assertEqual(self.ledger.summary()['physical_requests'], 2)

    def test_capability_and_invalid_input_reject_before_reservation(self):
        service = ModelService()
        service.register('chat', self.adapter({}, capabilities=[C.CHAT]))
        cases = [ModelRequest(C.IMAGE, {'prompt': 'x'}),
                 ModelRequest(C.CHAT, {'messages': []}),
                 ModelRequest(C.CHAT, {'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': True}),
                 ModelRequest(C.CHAT, {'messages': [{'role': 'user', 'content': 'x'}], 'model': 'override'})]
        for request in cases:
            with self.subTest(request=request), self.assertRaises((ValueError, RuntimeError)):
                service.invoke('chat', request)
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.ledger.summary()['physical_requests'], 0)

    def test_read_timeout_is_unknown_and_blocks_subsequent_request_without_retry(self):
        def timeout(request):
            raise httpx.ReadTimeout('synthetic', request=request)
        adapter = self.adapter(timeout)
        request = ModelRequest(C.IMAGE, {'prompt': 'x'})
        with self.assertRaisesRegex(RuntimeError, 'OUTCOME_UNKNOWN'):
            adapter.invoke(request)
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
            adapter.invoke(request)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.ledger.summary()['pending_or_unknown'], 1)
        self.assertEqual(self.ledger.summary()['effective_usage_or_reserved']['units'], 10)

    def test_http_error_and_malformed_success_are_terminal_and_never_retried(self):
        for response in [httpx.Response(429, json={'error': 'limited'}), httpx.Response(200, json={}),
                         httpx.Response(307, headers={'location': 'https://elsewhere.invalid'})]:
            adapter = self.adapter(lambda request: response)
            with self.subTest(status=response.status_code), self.assertRaises(RuntimeError):
                adapter.invoke(ModelRequest(C.CHAT, {'messages': [{'role': 'user', 'content': 'q'}]}))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.ledger.summary()['pending_or_unknown'], 0)
        self.assertEqual(self.ledger.summary()['effective_usage_or_reserved']['units'], 30)

    def test_cancellation_and_budget_refusal_do_not_send(self):
        adapter = self.adapter({})
        cancel = Event()
        cancel.set()
        with self.assertRaises(RuntimeError), execution_scope(deadline_seconds=10, cancellation=cancel):
            adapter.invoke(ModelRequest(C.IMAGE, {'prompt': 'x'}))
        adapter.reservation['requests'] = 31
        with self.assertRaisesRegex(ValueError, 'BUDGET_INSUFFICIENT'):
            adapter.invoke(ModelRequest(C.IMAGE, {'prompt': 'x'}))
        self.assertEqual(len(self.sent), 0)

    def test_call_context_timeout_reaches_transport(self):
        def reply(request):
            self.assertGreater(request.extensions['timeout']['read'], 0)
            self.assertLessEqual(request.extensions['timeout']['read'], 3)
            return httpx.Response(200, json={'data': [{'url': 'https://media.invalid/image.png'}]})
        result = self.adapter(reply).invoke(ModelRequest(C.IMAGE, {'prompt': 'x'}, {'remaining_seconds': 3}))
        self.assertEqual(result.data['images'][0]['url'], 'https://media.invalid/image.png')
        self.assertEqual(len(self.sent), 1)

    def test_registry_lease_prevents_unload_and_old_disposer_cannot_remove_replacement(self):
        service = ModelService()
        entered, release = Event(), Event()
        class SDK:
            info = ModelInfo('sdk', 'cpu-model', frozenset({C.SPEECH}))
            def invoke(inner, request):
                entered.set()
                release.wait(5)
                return ModelResult('sdk', 'cpu-model', C.SPEECH, {'media': Media(b'audio', 'audio/wav')})
        first = service.register('voice', SDK())
        worker = Thread(target=lambda: service.invoke('voice', ModelRequest(C.SPEECH, {'text': 'hello'})))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, 'MODEL_BUSY'):
                first()
            with self.assertRaisesRegex(ValueError, 'MODEL_BUSY'):
                service.close()
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        first()
        service.register('voice', SDK())
        first()
        self.assertIn('voice', service.catalog())

    def test_plugin_consumers_share_the_same_llm_service_without_knowledge(self):
        service = ModelService()
        service.register('generation', self.adapter({'choices': [{'message': {'content': '{"value":1}'},
                                                                  'finish_reason': 'stop'}]}))
        received = []
        def auxiliary(ctx, config):
            received.append(ctx.require('llm'))
            ctx.provide('summary', ctx.require('llm'))
        profile = {'version': 1, 'plugins': [
            {'id': 'models', 'use': 'models.service'}, {'id': 'llm', 'use': 'models.structured'},
            {'id': 'summary', 'use': 'summary'}]}
        with PluginHost(profile, (*model_plugins(service), PluginSpec('summary', ('summary',), ('llm',), auxiliary))) as host:
            self.assertIs(host.service('llm'), received[0])
            result = host.call('summary', 'complete', [{'role': 'user', 'content': 'q'}], role='summary',
                schema={'type': 'object'}, call_context={})
            self.assertEqual(result.value, {'value': 1})
        self.assertEqual(len(self.sent), 1)

    def test_existing_embedding_and_ocr_ports_are_routed_and_validate_results(self):
        class Embed:
            provider_id, model = 'google', 'synthetic'
            def documents(self, texts):
                return [[1., 2.] for _ in texts]
            def query(self, text):
                return [3., 4.]
        class OCR:
            provider_id, model = 'vision', 'synthetic'
            def complete_json(inner, system, user, **options):
                self.assertEqual(options['purpose'], 'ocr')
                self.assertEqual(options['images'], [b'\x89PNG synthetic'])
                result = {'blocks': [], 'complete': True}
                options['validator'](result)
                return result
        service = ModelService()
        service.register('embedding', EmbeddingAdapter(Embed()))
        service.register('ocr', LunaAdapter(OCR()))
        view = EmbeddingView(service, dimension=2)
        self.assertEqual(view.query('q'), [3., 4.])
        self.assertEqual(view.documents(['a']), [[1., 2.]])
        result = OCRView(service, 'ocr').complete_json('system', 'user', purpose='ocr',
            images=[b'\x89PNG synthetic'], validator=lambda value: self.assertTrue(value['complete']),
            contract_version='synthetic/1')
        self.assertTrue(result['complete'])

    def test_config_supports_multiple_providers_and_closes_without_credentials_in_catalog(self):
        config = {'version': 1, 'budget': {'path': str(self.root / 'configured.sqlite3'), 'caps': {'requests': 5}},
            'providers': {'a': {'adapter': 'openai-compatible', 'base_url': 'https://a.invalid/v1', 'api_key_env': 'SYNTH_KEY'},
                          'b': {'adapter': 'anthropic', 'base_url': 'https://b.invalid/v1', 'api_key_env': 'SYNTH_KEY'}},
            'models': {'generation': {'provider': 'b', 'model': 'chat', 'capabilities': ['chat'], 'reservation': {'requests': 1}},
                       'voice': {'provider': 'a', 'model': 'speech', 'capabilities': ['speech'], 'reservation': {'requests': 1}}}}
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        with patch.dict('os.environ', {'SYNTH_KEY': 'synthetic-secret'}):
            service = from_config(path)
        self.addCleanup(service.close)
        self.assertEqual(service.catalog()['voice'].capabilities, frozenset({C.SPEECH}))
        self.assertNotIn('synthetic-secret', repr(service.catalog()))

    def test_permission_error_blocks_same_provider_after_restart(self):
        from kb_platform.provider_admission import CaptureAdmissionError

        request = ModelRequest(C.CHAT, {'messages': [{'role': 'user', 'content': 'q'}]})
        first = self.adapter(lambda request: httpx.Response(403, json={'error': 'forbidden'}))
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_HTTP_ERROR'):
            first.invoke(request)
        second = self.adapter({})
        with self.assertRaises(CaptureAdmissionError):
            second.invoke(request)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.ledger.summary()['pending_or_unknown'], 0)

    def test_failed_connection_is_known_not_sent_and_releases_reservation(self):
        def fail(request):
            raise httpx.ConnectError('synthetic', request=request)
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_TRANSPORT_ERROR'):
            self.adapter(fail).invoke(ModelRequest(C.IMAGE, {'prompt': 'x'}))
        summary = self.ledger.summary()
        self.assertEqual(summary['pending_or_unknown'], 0)
        self.assertEqual(summary['effective_usage_or_reserved'], {'requests': 0, 'units': 0})

    def test_sdk_result_validation_and_stale_binding_are_enforced(self):
        service = ModelService()
        class BadSDK:
            info = ModelInfo('sdk', 'embed', frozenset({C.EMBEDDING}))
            def invoke(self, request):
                return ModelResult('sdk', 'embed', C.EMBEDDING, {'vectors': [[float('nan')]]})
        service.register('bad', BadSDK())
        with self.assertRaisesRegex(RuntimeError, 'INVALID_MODEL_RESULT'):
            service.invoke('bad', ModelRequest(C.EMBEDDING, {'texts': ['q']}))
        remove = service.register('chat', self.adapter({}))
        bound = service.structured('chat')
        remove()
        service.register('chat', self.adapter({}, provider='replacement'))
        with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
            bound.complete([], role='answer', schema={'type': 'object'}, call_context={})
        self.assertEqual(self.sent, [])

    def test_structured_binding_cannot_cross_registration_even_with_same_adapter(self):
        service = ModelService()
        adapter = self.adapter({'choices': [{'message': {'content': '{"answer":"ok"}'},
                                            'finish_reason': 'stop'}]})
        remove = service.register('chat', adapter)
        stale = service.structured('chat')
        remove()
        with self.assertRaisesRegex(RuntimeError, 'MODEL_ROUTE_UNAVAILABLE'):
            stale.complete([{'role': 'user', 'content': 'q'}], role='answer',
                           schema={'type': 'object'}, call_context={})
        service.register('chat', adapter)
        with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
            stale.complete([{'role': 'user', 'content': 'q'}], role='answer',
                           schema={'type': 'object'}, call_context={})
        self.assertEqual(self.sent, [])
        fresh = service.structured('chat')
        result = fresh.complete([{'role': 'user', 'content': 'q'}], role='answer',
                                schema={'type': 'object'}, call_context={})
        self.assertEqual(result.value, {'answer': 'ok'})
        self.assertEqual(len(self.sent), 1)

    def test_standalone_structured_deadline_is_rechecked_after_reservation(self):
        for elapsed in (5, 6, 2):
            with self.subTest(elapsed=elapsed):
                clock = [100.0]
                service = ModelService()
                service.register('chat', self.adapter({'choices': [{'message': {'content': '{}'},
                                                                    'finish_reason': 'stop'}]}))
                model = service.structured('chat')
                reserve = self.ledger.reserve
                def slow_reserve(*args, **kwargs):
                    result = reserve(*args, **kwargs)
                    clock[0] += elapsed
                    return result
                before = len(self.sent)
                usage_before = self.ledger.summary()['effective_usage_or_reserved']
                with patch('kb_platform.model_http.time.monotonic', side_effect=lambda: clock[0]), \
                        patch.object(self.ledger, 'reserve', side_effect=slow_reserve):
                    options = dict(role='answer', schema={'type': 'object'},
                                   call_context={'remaining_seconds': 5})
                    if elapsed >= 5:
                        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_DEADLINE_EXCEEDED'):
                            model.complete([{'role': 'user', 'content': 'q'}], **options)
                    else:
                        model.complete([{'role': 'user', 'content': 'q'}], **options)
                self.assertEqual(self.ledger.summary()['pending_or_unknown'], 0)
                if elapsed >= 5:
                    self.assertEqual(len(self.sent), before)
                    self.assertEqual(self.ledger.summary()['effective_usage_or_reserved'], usage_before)
                else:
                    self.assertEqual(len(self.sent), before + 1)
                    self.assertEqual(self.sent[-1].extensions['timeout']['read'], 3)

    def test_embedding_job_admission_refusal_is_retryable_without_circuit_failure(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        from kb_platform.embedding_jobs import EmbeddingJobs, EmbeddingJobError

        for scenario in ('pending', 'budget'):
            with self.subTest(scenario=scenario):
                ledger = BudgetLedger(self.root / (scenario + '.sqlite3'), {'requests': 1, 'units': 10})
                adapter = self.adapter({'data': [{'index': 0, 'embedding': [1., 2.]}]},
                                       capabilities=[C.EMBEDDING])
                adapter.ledger = ledger
                if scenario == 'pending':
                    ledger.reserve('other-chat', {'requests': 1, 'units': 10})
                else:
                    adapter.reservation['requests'] = 2
                models = ModelService()
                models.register('embedding', adapter)
                view = EmbeddingView(models, dimension=2)
                jobs = EmbeddingJobs('synthetic')
                row = dict(tenant_id='t', kb_id='k', build_id='b', batch_no=0, request_id='attempt-1',
                           provider_hash='p', attempt=1, status='RUNNING', payload={'units': [{'text': 'one'}]})
                conn = Mock()
                conn.execute.return_value.fetchone.return_value = {'retry_at': None}
                @contextmanager
                def tx(*args):
                    yield conn
                before = len(self.sent)
                summary = ledger.summary()
                with patch.object(jobs, '_tx', tx), \
                        patch.object(jobs, '_prepare', return_value=(view.manifest, 'dependency')), \
                        patch.object(jobs, '_claim', return_value=row), \
                        patch.object(jobs, 'status', return_value=[row]):
                    expected = 'EMBEDDING_LEDGER_BLOCKED' if scenario == 'pending' else 'EMBEDDING_BUDGET_INSUFFICIENT'
                    with self.assertRaisesRegex(EmbeddingJobError, expected):
                        jobs.run('t', 'k', 'b', view, [{'text': 'one'}], lambda: True, {})
                    calls = conn.execute.call_args_list
                    self.assertEqual(calls[0].args[1][0], 'RETRY')
                    self.assertEqual(calls[0].args[1][3], expected)
                    self.assertTrue(calls[0].args[1][4])
                    self.assertFalse(any('UPDATE embedding_circuits' in c.args[0] for c in calls))
                    self.assertEqual(len(self.sent), before)
                    self.assertEqual(ledger.summary(), summary)
                    if scenario == 'pending':
                        ledger.finish('other-chat', status='ERROR', usage={'requests': 0, 'units': 0})
                    else:
                        adapter.reservation['requests'] = 1
                    result = jobs.run('t', 'k', 'b', view, [{'text': 'one'}], lambda: True, {})
                    self.assertEqual(result[0]['vector'], [1., 2.])
                    self.assertEqual(len(self.sent), before + 1)
                    self.assertEqual(conn.execute.call_args_list[-3].args[1][0], 'COMPLETE')

    def test_generic_agent_uses_public_llm_dependency_and_persists_answer(self):
        from kb_platform.harness_profile import create_agent_application

        models = ModelService()
        models.register('generation', self.adapter({'choices': [{'message': {
            'content': '{"kind":"answer","value":"shared model"}'}, 'finish_reason': 'stop'}]}))
        profile = {'version': 1, 'bundles': ['core'], 'plugins': [
            {'id': 'store', 'config': {'root': str(self.root / 'sessions')}},
            {'id': 'models', 'use': 'models.service'}, {'id': 'llm', 'use': 'models.structured'},
            {'id': 'model', 'use': 'harness.model.service'}]}
        app, host = create_agent_application(profile=profile, models=models, environment={})
        from kb_platform.session_identity import Actor
        actor = Actor('tenant', 'user')
        with host:
            session = app.create(actor)
            result = app.turn(actor, session['session_id'], message='hello', request_id='one',
                              expected_revision=session['revision'])
            self.assertEqual(result['result'], 'shared model')
            self.assertIs(host.service('model').model, host.service('llm'))
        self.assertEqual(len(self.sent), 1)


    def test_endpoint_change_changes_embedding_identity(self):
        first = self.adapter({}, capabilities=[C.EMBEDDING])
        other = OpenAICompatibleAdapter(provider='vendor-a', model='model-a',
            endpoint='https://different.invalid/v1', api_key='synthetic', capabilities=[C.EMBEDDING],
            ledger=self.ledger, reservation={'requests': 1, 'units': 10})
        self.addCleanup(other.close)
        self.assertNotEqual(first.info.revision, other.info.revision)

    def test_capture_contains_request_hash_and_logical_identity_without_key_or_body(self):
        self.adapter({'data': [{'url': 'https://media.invalid/image.png'}]}).invoke(
            ModelRequest(C.IMAGE, {'prompt': 'PRIVATE PROMPT'}, {'call_id': 'logical-123'}))
        rows = [json.loads(path.read_text()) for path in (self.root / 'model-calls').glob('*.json')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['context']['call_id'], 'logical-123')
        self.assertEqual(rows[0]['status'], 'COMPLETE')
        self.assertEqual(len(rows[0]['payload_sha256']), 64)
        self.assertNotIn('PRIVATE PROMPT', json.dumps(rows))


    def test_embedding_and_ocr_bindings_reject_replacement_even_with_same_adapter(self):
        from kb_platform.embeddings import EmbeddingError

        for capability, reply in [(C.EMBEDDING, {'data': [{'index': 0, 'embedding': [1., 2.]}]}),
                                  (C.OCR, {'choices': [{'message': {'content': '{"text":"visible"}'}}]})]:
            with self.subTest(capability=capability):
                models = ModelService()
                adapter = self.adapter(reply, capabilities=[capability])
                remove = models.register('route', adapter)
                legacy = {'model': 'original', 'dimension': 2}
                view = (EmbeddingView(models, 'route', dimension=2, manifest=legacy)
                        if capability == C.EMBEDDING else OCRView(models, 'route', manifest=legacy))
                legacy['model'] = 'mutated outside'
                self.assertEqual(view.manifest['model'], 'original')
                remove()
                models.register('route', adapter)
                remove()
                self.assertIn('route', models.catalog())
                with self.assertRaisesRegex((EmbeddingError, RuntimeError), 'MODEL_BINDING_CHANGED'):
                    if capability == C.EMBEDDING:
                        view.query('q')
                    else:
                        view.complete_json('system', 'user', purpose='ocr', images=[b'\x89PNG synthetic'])
        self.assertEqual(self.sent, [])

    def test_embedding_http_failure_keeps_job_status_retry_hint_and_physical_identity(self):
        from kb_platform.embeddings import EmbeddingError
        from kb_platform.embedding_jobs import retry_delay

        models = ModelService()
        models.register('embedding', self.adapter(lambda request: httpx.Response(
            429, json={'error': 'rate limit'}, headers={'Retry-After': '19'}), capabilities=[C.EMBEDDING]))
        view = EmbeddingView(models, dimension=2)
        with self.assertRaises(EmbeddingError) as caught:
            view.documents(['one'])
        error = caught.exception
        self.assertEqual(error.status, 429)
        self.assertEqual(retry_delay(error, 1), 19)
        self.assertTrue(error.model_sent)
        self.assertTrue((self.root / 'model-calls' / (error.physical_request_id + '.json')).is_file())
        self.assertEqual(len(self.sent), 1)

    def test_embedding_unknown_and_budget_errors_remain_domain_failures_without_resend(self):
        from kb_platform.embeddings import EmbeddingError

        def timeout(request):
            raise httpx.ReadTimeout('synthetic', request=request)
        models = ModelService()
        models.register('embedding', self.adapter(timeout, capabilities=[C.EMBEDDING]))
        view = EmbeddingView(models, dimension=2)
        with self.assertRaisesRegex(EmbeddingError, 'PROVIDER_OUTCOME_UNKNOWN'):
            view.query('q')
        with self.assertRaisesRegex(EmbeddingError, 'UNRESOLVED_PHYSICAL_REQUEST'):
            view.query('q')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.ledger.summary()['pending_or_unknown'], 1)

    def test_embedding_job_context_is_correlated_without_recording_authority_content(self):
        models = ModelService()
        models.register('embedding', self.adapter({'data': [{'index': 0, 'embedding': [1., 2.]}]},
                                                 capabilities=[C.EMBEDDING]))
        view = EmbeddingView(models, dimension=2)
        with self.assertRaisesRegex(ValueError, 'EMBEDDING_AUTHORIZATION_FAILED'):
            view.documents(['private'], request_id='denied', authorize=lambda: False)
        self.assertEqual(self.sent, [])
        self.assertEqual(view.documents(['private'], request_id='job-attempt-1',
            audit_context={'scope': 'private-scope'}, authorize=lambda: True), [[1., 2.]])
        row = json.loads(next((self.root / 'model-calls').glob('*.json')).read_text())
        self.assertEqual(row['context']['request_id'], 'job-attempt-1')
        self.assertEqual(row['context']['purpose'], 'embedding_document')
        self.assertEqual(len(row['context']['audit_context_sha256']), 64)
        self.assertNotIn('private', json.dumps(row))

    def test_embedding_legacy_batch_hint_and_dimension_preflight_survive_wrapping(self):
        from kb_platform.embeddings import EmbeddingError
        from kb_platform.embedding_jobs import batch_size

        from kb_platform.remote import GoogleEmbeddingProvider

        provider = object.__new__(GoogleEmbeddingProvider)
        provider.platform, provider.model, provider.dimension = 'gemini_api', 'fixed', 2
        provider.documents = lambda texts: self.fail('A dimension mismatch must be rejected before sending')
        models = ModelService()
        models.register('embedding', EmbeddingAdapter(provider))
        view = EmbeddingView(models, dimension=3)
        self.assertEqual(batch_size(view), 16)
        with self.assertRaisesRegex(EmbeddingError, 'EMBEDDING_DIMENSION_MISMATCH'):
            view.documents(['one'])

    def test_document_plugins_run_sdk_ocr_and_embedding_without_chat_or_knowledge_agent(self):
        import io
        from PIL import Image
        from kb_platform.documents import parse_native_document
        from kb_platform.ocr import augment_ocr

        class LocalOCR:
            info = ModelInfo('local-ocr', 'fixed', frozenset({C.OCR}), frozenset({'image'}))
            def invoke(inner, request):
                self.assertEqual(request.call_context['purpose'], 'ocr')
                return ModelResult('local-ocr', 'fixed', C.OCR, {'complete': True, 'unreadable': [],
                    'blocks': [{'text': 'Do not exceed 5 mg.', 'bbox': [0.1, 0.1, 0.9, 0.9]}]})
        models = ModelService()
        models.register('ocr', LocalOCR())
        models.register('embedding', self.adapter({'data': [{'index': 0, 'embedding': [1., 2.]}]},
                                                 capabilities=[C.EMBEDDING]))
        profile = {'version': 1, 'plugins': [
            {'id': 'models', 'use': 'models.service'}, {'id': 'ocr', 'use': 'models.ocr'},
            {'id': 'embedding', 'use': 'models.embedding', 'config': {'dimension': 2}}]}
        stream = io.BytesIO()
        Image.new('RGB', (100, 50), 'white').save(stream, format='PNG')
        original = stream.getvalue()
        native = parse_native_document(original, 'scan.png', {'ocr': True})
        with PluginHost(profile, model_plugins(models)) as host:
            parsed = augment_ocr(original, 'scan.png', native, host.service('ocr'))
            self.assertEqual(parsed['status'], 'READY')
            self.assertEqual(parsed['original_sha256'], native['original_sha256'])
            self.assertNotEqual(parsed['parse_snapshot_id'], native['parse_snapshot_id'])
            self.assertEqual(parsed['elements'][0]['raw_text'], 'Do not exceed 5 mg.')
            self.assertEqual(parsed['anchors'][0]['quote'], 'Do not exceed 5 mg.')
            self.assertEqual(parsed['anchors'][0]['normalized_bbox'], [0.1, 0.1, 0.9, 0.9])
            self.assertEqual(parsed['quality_report']['ocr_provider']['provider'], 'local-ocr')
            self.assertEqual(parsed['quality_report']['model_call_observation'], 'logical_invocations')
            self.assertEqual(parsed['quality_report']['ocr_protocol']['maximum_format_corrections'], 0)
            self.assertEqual(host.call('embedding', 'documents', ['Do not exceed 5 mg.']), [[1., 2.]])
        self.assertIn('ocr', models.catalog())



class ModelRouteLifecycleContracts(unittest.TestCase):
    @staticmethod
    def adapter(*, close=None):
        class Adapter:
            info = ModelInfo('local', 'echo', frozenset({C.CHAT}))

            def invoke(self, request):
                return ModelResult('local', 'echo', C.CHAT, {'text': 'ok'})

        instance = Adapter()
        if close is not None:
            instance.close = close
        return instance

    @staticmethod
    def request():
        return ModelRequest(C.CHAT, {'messages': [{'role': 'user', 'content': 'hello'}]})

    def test_same_adapter_re_registration_and_independent_route_keep_identity(self):
        models = ModelService()
        adapter = self.adapter()
        dispose = models.register('generation', adapter)
        models.register('independent', adapter)
        old = models.bind('generation', C.CHAT)
        independent = models.bind('independent', C.CHAT)
        dispose()
        models.register('generation', adapter)
        current = models.bind('generation', C.CHAT)
        self.assertNotEqual(old.registration_id, current.registration_id)
        with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
            old.invoke(self.request())
        self.assertEqual(independent.invoke(self.request()).data, {'text': 'ok'})
        self.assertEqual(current.invoke(self.request()).data, {'text': 'ok'})

    def test_failed_cleanup_remains_visible_and_retry_skips_completed_routes(self):
        models, closed, failures = ModelService(), [], [True]

        def failing():
            closed.append('failed')
            if failures.pop(0) if failures else False:
                raise RuntimeError('synthetic close failure')

        models.register('failed', self.adapter(close=failing), owned=True)
        models.register('complete', self.adapter(close=lambda: closed.append('complete')), owned=True)
        with self.assertRaisesRegex(RuntimeError, 'synthetic close failure'):
            models.close()
        state = models.inspect()
        self.assertEqual(set(state), {'failed'})
        self.assertEqual(state['failed']['phase'], 'cleanup_failed')
        self.assertEqual(state['failed']['cleanup_error'], 'RuntimeError')
        with self.assertRaisesRegex(RuntimeError, 'MODEL_ROUTE_UNAVAILABLE'):
            models.invoke('failed', self.request())
        models.close()
        self.assertEqual(closed, ['complete', 'failed', 'failed'])
        self.assertEqual(models.inspect(), {})

    def test_adapter_close_and_owner_lease_run_without_registry_lock(self):
        from contextlib import contextmanager

        models, observed = ModelService(), []

        def concurrent_inspection():
            finished = Event()
            thread = Thread(target=lambda: (models.inspect(), finished.set()), daemon=True)
            thread.start()
            self.assertTrue(finished.wait(2), 'callback holds the model registry lock')
            thread.join(2)
            observed.append('inspected')

        @contextmanager
        def ownership():
            concurrent_inspection()
            yield

        dispose = models.register('generation', self.adapter(close=concurrent_inspection), owned=True,
            registration_id=1, owner_identity={'runtime': 7, 'instance': 2, 'generation': 1},
            owner_lease=ownership)
        self.assertEqual(models.invoke('generation', self.request()).data, {'text': 'ok'})
        dispose()
        self.assertEqual(observed, ['inspected', 'inspected'])

    def test_explicit_registration_identity_cannot_be_reused_after_cleanup(self):
        models = ModelService()
        dispose = models.register('generation', self.adapter(), registration_id='fixed')
        dispose()
        with self.assertRaisesRegex(ValueError, 'MODEL_REGISTRATION_ID_REUSED'):
            models.register('generation', self.adapter(), registration_id='fixed')

    def test_runtime_route_plugin_disable_preserves_other_routes_and_registry(self):
        class Client:
            provider_id, model = 'local', 'echo'

            def complete_json(self, system, user, **kwargs):
                return {'value': 1}

        profile = {'version': 1, 'plugins': [
            {'id': 'models', 'use': 'models.service'}, {'id': 'clients', 'use': 'models.clients'},
            {'id': 'route-a', 'use': 'models.route.client',
             'config': {'route': 'a', 'client': 'raw', 'capability': 'chat'}},
            {'id': 'route-b', 'use': 'models.route.client',
             'config': {'route': 'b', 'client': 'raw', 'capability': 'chat'}}]}
        with PluginHost(profile, model_plugins(clients={'raw': Client()})) as host:
            models = host.service('models')
            old = models.bind('a', C.OCR)
            other = models.bind('b', C.OCR)
            request = ModelRequest(C.OCR, {'media': Media(b'image', 'image/png')})
            self.assertEqual(old.invoke(request).data, {'value': 1})
            host.update('route-a', enabled=False)
            self.assertIs(host.service('models'), models)
            with self.assertRaisesRegex(RuntimeError, 'MODEL_ROUTE_UNAVAILABLE'):
                old.invoke(request)
            self.assertEqual(other.invoke(request).data, {'value': 1})
            host.update('route-a', enabled=True)
            with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
                old.invoke(request)
            self.assertNotEqual(models.bind('a', C.OCR).registration_id, old.registration_id)

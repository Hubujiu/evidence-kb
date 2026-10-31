from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from kb_platform.agent_model import BudgetLedger as AgentBudget
from kb_platform.context_budget import ContextBudget
from kb_platform.embedding_jobs import EmbeddingJobs, EmbeddingJobError, recovered_vectors
from kb_platform.embeddings import EmbeddingCallError, EmbeddingError
from kb_platform.google_semantic_provider import GoogleSemanticProvider
from kb_platform.model_adapters import EmbeddingAdapter
from kb_platform.model_configuration import EmbeddingView
from kb_platform.model_http import OpenAICompatibleAdapter
from kb_platform.model_plugins import model_plugins
from kb_platform.model_service import Capability, ModelInfo, ModelRequest, ModelResult, ModelService, validate_request
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.rag_testing.budget import BudgetLedger, producer_guard
from kb_platform.remote import GoogleEmbeddingProvider


class ModelFixtures:
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sent = []

    def adapter(self, name='model', respond=None):
        ledger = BudgetLedger(self.root / (name + '.sqlite3'), {'requests': 1})
        def handle(request):
            self.sent.append(request)
            if respond:
                return respond(request)
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return OpenAICompatibleAdapter(provider='fixture', model='fixed', endpoint='https://fixture.invalid/v1',
            api_key='synthetic', capabilities=[Capability.CHAT, Capability.EMBEDDING], ledger=ledger,
            reservation={'requests': 1}, client=client)

    def view(self, adapter, *, dimension=2, manifest=None):
        models = ModelService()
        self.addCleanup(models.close)
        dispose = models.register('embedding', adapter)
        return EmbeddingView(models, dimension=dimension, manifest=manifest), dispose

    def observed(self):
        path = self.root / 'google.json'
        path.write_text(json.dumps({'google': {'base_url': 'https://fixture.invalid/v1',
            'api_key': 'synthetic', 'model': 'gemini-embedding-2', 'dimension': 768,
            'cache_mode': 'disabled', 'retries': 0}, 'cache_dir': str(self.root / 'cache'),
            'log_path': str(self.root / 'calls.jsonl')}), 'utf-8')
        def remote(config):
            provider = GoogleEmbeddingProvider(config)
            provider.client.close()
            def handle(request):
                self.sent.append(request)
                payload = json.loads(request.content)
                vector = {'values': [1.] + [0.] * 767}
                body = ({'embeddings': [vector for _ in payload['requests']]}
                        if 'requests' in payload else {'embedding': vector})
                return httpx.Response(200, json={**body, 'usageMetadata': {'promptTokenCount': 3}})
            provider.client = httpx.Client(transport=httpx.MockTransport(handle))
            return provider
        ledger = BudgetLedger(self.root / 'observed.sqlite3', {'remote_embedding_calls': 3, 'input_tokens': 24576})
        provider = GoogleSemanticProvider(path, ledger=ledger, audit_dir=self.root / 'audit', remote_factory=remote)
        self.addCleanup(provider.close)
        return provider

    @staticmethod
    def audit():
        return {'tenant_id': 't', 'kb_id': 'k', 'build_id': 'b', 'principal': 'owner', 'acl_revision': '1',
            'sources': [{'source_id': 's', 'source_version_id': 'v', 'parse_snapshot_id': 'p',
                         'source_text_sha256': 'frozen-source'}]}


class ModelReviewContracts(ModelFixtures, unittest.TestCase):
    def test_request_data_copy_preserves_authorization_capability_identity(self):
        class Authorizer:
            def __call__(self):
                return True
        authorize = Authorizer()
        request = ModelRequest(Capability.EMBEDDING, {'texts': ['source']},
                               {'authorize': authorize, 'audit_context': {'sources': ['original']}})
        copied = validate_request(request)
        request.payload['texts'].append('changed')
        request.call_context['audit_context']['sources'].append('changed')
        self.assertIs(copied.call_context['authorize'], authorize)
        self.assertEqual(copied.payload['texts'], ['source'])
        self.assertEqual(copied.call_context['audit_context']['sources'], ['original'])

    def test_observed_pre_send_revocation_uses_original_bound_and_callable_authorizers(self):
        provider = self.observed()
        view, _ = self.view(EmbeddingAdapter(provider), dimension=768)
        for binding in ('method', 'callable'):
            with self.subTest(binding=binding):
                class Authorizer:
                    allowed = True
                    def check(self):
                        return self.allowed
                    def __call__(self):
                        return self.check()
                authorizer = Authorizer()
                authorize = authorizer.check if binding == 'method' else authorizer
                reserve = provider.ledger.reserve
                def revoke_after_reservation(*args, **kwargs):
                    result = reserve(*args, **kwargs)
                    authorizer.allowed = False
                    return result
                request_id = 'revoke_' + binding
                with patch.object(provider.ledger, 'reserve', side_effect=revoke_after_reservation), \
                        patch.object(provider.remote, '_post', wraps=provider.remote._post) as post:
                    with self.assertRaisesRegex(EmbeddingError, 'authorization required'):
                        view.documents(['private source'], request_id=request_id, audit_context=self.audit(),
                                       authorize=authorize)
                    self.assertEqual(post.call_count, 0)
                record = json.loads((provider.audit_dir / (request_id + '.json')).read_text('utf-8'))
                self.assertEqual(record['status'], 'ERROR')
                self.assertEqual(record['physical_embedding_attempts'], 0)
                self.assertEqual(record['usage'], {'remote_embedding_calls': 0, 'input_tokens': 0})
                self.assertEqual(provider.ledger.summary()['pending_or_unknown'], 0)
                self.assertEqual(self.sent, [])

    def test_embedding_jobs_known_unsent_transport_retries_without_attempt_or_circuit_failure(self):
        for transport_error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ReadTimeout):
            with self.subTest(error=transport_error.__name__):
                def respond(request):
                    raise transport_error('synthetic transport failure', request=request)
                adapter = self.adapter(transport_error.__name__, respond)
                view, _ = self.view(adapter)
                jobs = EmbeddingJobs('synthetic', max_attempts=1, circuit_threshold=1)
                row = dict(tenant_id='t', kb_id='k', build_id='b', batch_no=0, request_id='attempt-1',
                           provider_hash='p', attempt=1, status='RUNNING', payload={'units': [{'text': 'one'}]})
                connection = Mock()
                connection.execute.return_value.fetchone.return_value = {'retry_at': None}
                @contextmanager
                def transaction(*args):
                    yield connection
                unsent = transport_error is not httpx.ReadTimeout
                with patch.object(jobs, '_tx', transaction), \
                        patch.object(jobs, '_prepare', return_value=(view.manifest, 'dependency')), \
                        patch.object(jobs, '_claim', return_value=row), \
                        patch.object(jobs, 'status', return_value=[row]):
                    with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY' if unsent else 'EMBEDDING_UNKNOWN'):
                        jobs.run('t', 'k', 'b', view, [{'text': 'one'}], lambda: True, {})
                statements = connection.execute.call_args_list
                self.assertEqual(statements[0].args[1][0], 'RETRY' if unsent else 'UNKNOWN')
                self.assertEqual(statements[0].args[1][4], unsent)
                self.assertEqual(any('UPDATE embedding_circuits' in call.args[0] for call in statements), not unsent)
                self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 0 if unsent else 1)
                self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 0 if unsent else 1})

    def test_owner_admission_failure_and_route_change_do_not_leak_domain_use(self):
        for scenario in ('failed', 'replaced', 'adopted'):
            with self.subTest(scenario=scenario):
                models, invoked, remove = ModelService(), [], []
                self.addCleanup(models.close)
                class Adapter:
                    info = ModelInfo('fixture', 'fixed', frozenset({Capability.CHAT}))
                    def invoke(self, request):
                        invoked.append(request)
                        return ModelResult('fixture', 'fixed', Capability.CHAT, {'text': 'answer'})
                @contextmanager
                def ownership():
                    if scenario == 'failed':
                        raise RuntimeError('owner admission failed')
                    if scenario == 'adopted':
                        models.adopt(SimpleNamespace(identity={'runtime': 2},
                            registration_id=lambda **options: 'new-owner', lease=nullcontext))
                    else:
                        remove[0]()
                        models.register('generation', Adapter())
                    yield
                remove.append(models.register('generation', Adapter(), owner_lease=ownership))
                bound = models.bind('generation', Capability.CHAT)
                with self.assertRaisesRegex(RuntimeError,
                        'owner admission failed' if scenario == 'failed' else 'MODEL_BINDING_CHANGED'):
                    bound.invoke(ModelRequest(Capability.CHAT,
                        {'messages': [{'role': 'user', 'content': 'question'}]}))
                self.assertEqual(invoked, [])
                self.assertTrue(all(entry['active_calls'] == 0 for entry in models.inspect().values()))

    def test_raw_bound_invocation_releases_domain_use_before_owner_cleanup_pump(self):
        entered, release, results, errors = Event(), Event(), [], []
        class Adapter:
            info = ModelInfo('fixture', 'fixed', frozenset({Capability.CHAT}))
            def invoke(self, request):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('blocked model was never released')
                return ModelResult('fixture', 'fixed', Capability.CHAT, {'text': 'answer'})
        def route(context, config):
            remove = context.require('models').register('generation', Adapter(),
                registration_id=context.registration_id(kind='model_route'), owner_identity=context.identity,
                owner_lease=context.lease)
            context.defer(remove)
        profile = {'version': 1, 'plugins': [{'id': 'models', 'use': 'models.service'},
                                            {'id': 'route', 'use': 'fixture.route'}]}
        with PluginHost(profile, (*model_plugins(), PluginSpec('fixture.route', (), ('models',), route))) as host:
            models = host.service('models')
            bound = models.bind('generation', Capability.CHAT)
            def invoke():
                try:
                    results.append(bound.invoke(ModelRequest(Capability.CHAT,
                        {'messages': [{'role': 'user', 'content': 'question'}]})))
                except BaseException as error:
                    errors.append(error)
            thread = Thread(target=invoke, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                host.update('route', enabled=False)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([result.data for result in results], [{'text': 'answer'}])
            self.assertEqual(models.inspect(), {})
            self.assertNotEqual(host.inspect()['state'], 'cleanup_failed')

    def test_real_producer_busy_and_sealed_are_zero_effect_model_preflight(self):
        for state in ('BUSY', 'SEALED'):
            with self.subTest(state=state):
                adapter = self.adapter(state)
                view, _ = self.view(adapter)
                captures_before = set(adapter.audit_root.glob('*.json'))
                if state == 'SEALED':
                    with producer_guard(adapter.ledger.path) as producer:
                        producer.execute("INSERT INTO producer VALUES (1,'successor')")
                guard = producer_guard(adapter.ledger.path) if state == 'BUSY' else nullcontext()
                with guard:
                    with self.assertRaises(EmbeddingCallError) as raised:
                        view.documents(['one'])
                self.assertEqual(raised.exception.preflight_code, 'BUDGET_PRODUCER_' + state)
                self.assertIs(raised.exception.model_sent, False)
                self.assertEqual(adapter.ledger.summary()['physical_requests'], 0)
                self.assertEqual(set(adapter.audit_root.glob('*.json')), captures_before)
                self.assertEqual(self.sent, [])
                if state == 'BUSY':
                    self.assertEqual(view.documents(['one']), [[1., 0.]])
                    self.assertEqual(len(self.sent), 1)
                    self.sent.clear()

    def test_known_unsent_transport_releases_physical_and_upper_turn_budgets(self):
        for transport_error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            with self.subTest(error=transport_error.__name__):
                failure = [True]
                def respond(request):
                    if failure[0]:
                        raise transport_error('synthetic connection failure', request=request)
                    return httpx.Response(200, json={'choices': [{'message': {'content': '{"answer":"ok"}'},
                        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 2, 'total_tokens': 4}})
                adapter = self.adapter(transport_error.__name__, respond)
                models = ModelService()
                self.addCleanup(models.close)
                models.register('chat', adapter)
                budget = AgentBudget(max_calls=1, max_tokens=4096)
                structured = models.structured('chat', ledger=budget,
                    policy=ContextBudget(input_budget=1024, model_window=4096, output_reserve=64))
                options = {'role': 'answer', 'schema': {'type': 'object'}, 'output_limit': 16,
                    'call_context': {'session_id': 's', 'turn_id': 't', 'max_physical_calls': 1}}
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'PROVIDER_TRANSPORT_ERROR') as raised:
                        structured.complete([{'role': 'user', 'content': 'question'}], **options)
                    self.assertIs(raised.exception.model_sent, False)
                    self.assertEqual(raised.exception.call_record['attempts'], 0)
                    for snapshot in (budget.snapshot(), structured._model.turn_budget_snapshot('s', 't')):
                        self.assertEqual({key: snapshot[key] for key in ('calls', 'tokens', 'accounted_tokens',
                            'unknown_usage_calls', 'reserved_calls', 'reserved_tokens')}, dict.fromkeys(
                                ('calls', 'tokens', 'accounted_tokens', 'unknown_usage_calls', 'reserved_calls', 'reserved_tokens'), 0))
                self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 0})
                self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 0)
                failure[0] = False
                self.assertEqual(structured.complete([{'role': 'user', 'content': 'question'}], **options).value,
                                 {'answer': 'ok'})
                self.assertEqual(budget.snapshot()['calls'], 1)
                self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 1})

    def test_observed_documents_keep_raw_identity_audit_authorization_and_manifest(self):
        provider = self.observed()
        view, _ = self.view(EmbeddingAdapter(provider), dimension=768)
        audit, authorized = self.audit(), []
        def authorize():
            authorized.append(True)
            return True
        vectors = view.documents(['source text'], request_id='document_identity', audit_context=audit, authorize=authorize)
        record = json.loads((provider.audit_dir / 'document_identity.json').read_text('utf-8'))
        self.assertEqual(view.manifest, provider.manifest)
        self.assertEqual(record['audit_context'], audit)
        self.assertEqual(record['physical_request_id'], 'document_identity')
        self.assertEqual(record['vectors'], vectors)
        self.assertGreaterEqual(len(authorized), 4)
        self.assertEqual(provider.ledger.summary()['physical_requests'], 1)
        self.assertEqual(len(self.sent), 1)
        with self.assertRaisesRegex(ValueError, 'EMBEDDING_AUTHORIZATION_FAILED'):
            view.documents(['denied'], request_id='denied', audit_context=audit, authorize=lambda: False)
        self.assertEqual(len(self.sent), 1)

    def test_observed_query_keeps_explicit_request_context(self):
        provider = self.observed()
        view, _ = self.view(EmbeddingAdapter(provider), dimension=768)
        vector = view.query('query', request_id='query_identity', audit_context=self.audit(), authorize=lambda: True)
        record = json.loads((provider.audit_dir / 'query_identity.json').read_text('utf-8'))
        self.assertEqual(record['task'], 'query')
        self.assertEqual(record['vectors'], [vector])
        self.assertEqual(record['audit_context'], self.audit())
        self.assertEqual(len(self.sent), 1)

    def test_observed_port_retains_producer_preflight_classification(self):
        provider = self.observed()
        view, _ = self.view(EmbeddingAdapter(provider), dimension=768)
        with producer_guard(provider.ledger.path):
            with self.assertRaises(EmbeddingCallError) as raised:
                view.documents(['source text'], request_id='busy_observed', audit_context=self.audit(),
                               authorize=lambda: True)
        self.assertEqual(raised.exception.preflight_code, 'BUDGET_PRODUCER_BUSY')
        self.assertIs(raised.exception.model_sent, False)
        self.assertEqual(provider.ledger.summary()['physical_requests'], 0)
        self.assertFalse((provider.audit_dir / 'busy_observed.json').exists())
        self.assertEqual(self.sent, [])

    def test_observed_recovery_uses_settled_capture_without_resend_and_rejects_stale_route(self):
        provider = self.observed()
        vectors = provider.documents(['source text'], request_id='recover_identity', audit_context=self.audit(),
                                     authorize=lambda: True)
        adapter = EmbeddingAdapter(provider)
        view, dispose = self.view(adapter, dimension=768, manifest=provider.manifest)
        row = {'request_id': 'recover_identity', 'payload': {'units': [{'text': 'source text'}],
            'manifest': deepcopy(provider.manifest), 'audit_context': self.audit()}}
        self.assertEqual(recovered_vectors(row, view), vectors)
        capture = provider.audit_dir / 'recover_identity.json'
        original = json.loads(capture.read_text('utf-8'))
        for field in ('request_payload_sha256', 'vectors_sha256', 'physical_request_id'):
            modified = {**original, field: 'changed'}
            capture.write_text(json.dumps(modified), 'utf-8')
            self.assertIsNone(recovered_vectors(row, view))
        capture.write_text(json.dumps(original), 'utf-8')
        with closing(sqlite3.connect(provider.ledger.path)) as ledger, ledger:
            ledger.execute("UPDATE calls SET status='UNKNOWN'")
        self.assertIsNone(recovered_vectors(row, view))
        with closing(sqlite3.connect(provider.ledger.path)) as ledger, ledger:
            ledger.execute("UPDATE calls SET status='COMPLETE'")
        altered = deepcopy(row)
        altered['payload']['audit_context']['acl_revision'] = 'changed'
        self.assertIsNone(recovered_vectors(altered, view))
        dispose()
        view.models.register('embedding', adapter)
        self.assertIsNone(recovered_vectors(row, view))
        self.assertEqual(len(self.sent), 1)

    def test_plain_google_texts_only_adapter_stays_supported(self):
        observed = self.observed()
        plain = observed.remote
        plain_view, _ = self.view(EmbeddingAdapter(plain), dimension=768)
        self.assertEqual(len(plain_view.query('query')), 768)
        self.assertEqual(len(plain_view.documents(['source text'])), 1)
        self.assertEqual(len(self.sent), 2)


def live_contract_suite(dsn):
    class LiveModelReviewContracts(ModelFixtures, unittest.TestCase):
        def setUp(self):
            super().setUp()
            self.scope = ('model-review-' + uuid4().hex, 'kb', 'build')
            self.jobs = EmbeddingJobs(dsn)

        def audit(self):
            return {**super().audit(), 'tenant_id': self.scope[0], 'kb_id': self.scope[1], 'build_id': self.scope[2]}

        def test_known_unsent_transport_errors_resume_with_new_physical_id_without_circuit_failure(self):
            for transport_error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                with self.subTest(error=transport_error.__name__):
                    self.scope = ('model-review-' + uuid4().hex, 'kb', 'build')
                    self.jobs = EmbeddingJobs(dsn, max_attempts=1, circuit_threshold=1)
                    failing = [True]
                    def respond(request):
                        if failing[0]:
                            raise transport_error('synthetic connection failure', request=request)
                        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})
                    adapter = self.adapter(transport_error.__name__, respond)
                    view, _ = self.view(adapter)
                    requests = []
                    for _ in range(2):
                        with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
                            self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())
                        row = self.jobs.status(*self.scope)[0]
                        self.assertEqual((row['status'], row['attempt']), ('RETRY', 0))
                        requests.append(row['request_id'])
                        with self.jobs._tx(*self.scope[:2]) as connection:
                            circuit = connection.execute('SELECT failures,open_until FROM embedding_circuits '
                                'WHERE tenant_id=%s AND kb_id=%s', self.scope[:2]).fetchone()
                            attempts = connection.execute('SELECT status FROM embedding_attempts '
                                'WHERE tenant_id=%s AND kb_id=%s', self.scope[:2]).fetchall()
                        self.assertEqual(circuit, {'failures': 0, 'open_until': None})
                        self.assertEqual([attempt['status'] for attempt in attempts], ['ERROR'] * len(requests))
                        self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 0)
                        self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 0})
                    failing[0] = False
                    self.assertEqual(len(self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())), 1)
                    row = self.jobs.status(*self.scope)[0]
                    requests.append(row['request_id'])
                    self.assertEqual((row['status'], row['attempt']), ('COMPLETE', 1))
                    self.assertEqual(len(set(requests)), 3)
                    self.assertEqual(adapter.ledger.summary()['physical_requests'], 3)
                    self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 1})

        def test_sent_transport_timeout_stays_unknown_and_is_not_automatically_retried(self):
            def respond(request):
                raise httpx.ReadTimeout('synthetic outcome unknown', request=request)
            adapter = self.adapter('unknown', respond)
            view, _ = self.view(adapter)
            for _ in range(2):
                with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_UNKNOWN'):
                    self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())
            row = self.jobs.status(*self.scope)[0]
            self.assertEqual((row['status'], row['attempt']), ('UNKNOWN', 1))
            with self.jobs._tx(*self.scope[:2]) as connection:
                attempts = connection.execute('SELECT status FROM embedding_attempts WHERE tenant_id=%s AND kb_id=%s',
                                              self.scope[:2]).fetchall()
                circuit = connection.execute('SELECT failures FROM embedding_circuits WHERE tenant_id=%s AND kb_id=%s',
                                             self.scope[:2]).fetchone()
            self.assertEqual(attempts, [{'status': 'UNKNOWN'}])
            self.assertEqual(circuit['failures'], 1)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 1)

        def test_producer_busy_is_retry_without_attempt_or_circuit_failure_and_resumes(self):
            adapter = self.adapter()
            view, _ = self.view(adapter)
            with producer_guard(adapter.ledger.path):
                with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEDGER_BLOCKED'):
                    self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())
            row = self.jobs.status(*self.scope)[0]
            self.assertEqual((row['status'], row['attempt'], row['error']), ('RETRY', 0, 'EMBEDDING_LEDGER_BLOCKED'))
            with self.jobs._tx(*self.scope[:2]) as connection:
                self.assertEqual(connection.execute('SELECT failures FROM embedding_circuits').fetchone()['failures'], 0)
            self.assertEqual(adapter.ledger.summary()['physical_requests'], 0)
            self.assertEqual(self.sent, [])
            self.assertEqual(len(self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())), 1)
            self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'COMPLETE')
            self.assertEqual(len(self.sent), 1)

        def test_sealed_producer_stays_retry_without_physical_or_circuit_effects(self):
            adapter = self.adapter()
            view, _ = self.view(adapter)
            with producer_guard(adapter.ledger.path) as producer:
                producer.execute("INSERT INTO producer VALUES (1,'successor')")
            for _ in range(2):
                with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEDGER_BLOCKED'):
                    self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())
                row = self.jobs.status(*self.scope)[0]
                self.assertEqual((row['status'], row['attempt']), ('RETRY', 0))
            with self.jobs._tx(*self.scope[:2]) as connection:
                self.assertEqual(connection.execute('SELECT failures FROM embedding_circuits').fetchone()['failures'], 0)
            self.assertEqual(adapter.ledger.summary()['physical_requests'], 0)
            self.assertEqual(self.sent, [])

        def test_observed_complete_capture_reconciles_crash_without_resend(self):
            provider = self.observed()
            view, _ = self.view(EmbeddingAdapter(provider), dimension=768, manifest=provider.manifest)
            _, dependency = self.jobs._prepare(*self.scope, view, [{'text': 'one'}], self.audit())
            row = self.jobs._claim(*self.scope, 0, dependency)
            view.documents(['one'], request_id=row['request_id'], audit_context=self.audit(), authorize=lambda: True)
            with self.jobs._tx(*self.scope[:2]) as connection:
                connection.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
            with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_AUTHORIZATION_FAILED'):
                self.jobs.reconcile(*self.scope, view, lambda: False)
            self.assertEqual(self.jobs.reconcile(*self.scope, view, lambda: True)[0]['status'], 'COMPLETE')
            self.assertEqual(len(self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())), 1)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(provider.ledger.summary()['physical_requests'], 1)

    return unittest.defaultTestLoader.loadTestsFromTestCase(LiveModelReviewContracts)


if __name__ == '__main__':
    unittest.main()

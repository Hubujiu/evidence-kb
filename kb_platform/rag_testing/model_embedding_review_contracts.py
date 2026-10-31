from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from kb_platform.agent_model import BudgetLedger as AgentBudget
from kb_platform.context_budget import ContextBudget
from kb_platform.embedding_jobs import EmbeddingJobError, EmbeddingJobs
from kb_platform.embeddings import EmbeddingCallError, EmbeddingError
from kb_platform.google_semantic_provider import GoogleSemanticProvider
from kb_platform.model_adapters import EmbeddingAdapter
from kb_platform.model_configuration import EmbeddingView
from kb_platform.model_http import OpenAICompatibleAdapter
from kb_platform.model_service import Capability, ModelRequest, ModelService, validate_request
from kb_platform.rag_testing.budget import BudgetLedger
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


class ModelEmbeddingReviewContracts(ModelFixtures, unittest.TestCase):
    def test_observed_recovery_keeps_capture_and_physical_state_without_resend(self):
        provider = self.observed()
        view, dispose = self.view(EmbeddingAdapter(provider), dimension=768)
        vectors = view.documents(['source text'], request_id='recover_identity', audit_context=self.audit(),
                                 authorize=lambda: True)
        observation = view.recover_embedding('recover_identity')
        self.assertEqual(observation['physical_status'], 'COMPLETE')
        self.assertEqual(observation['record']['vectors'], vectors)
        self.assertEqual(observation['record']['audit_context'], self.audit())
        capture = provider.audit_dir / 'recover_identity.json'
        original = capture.read_text('utf-8')
        changed = json.loads(original)
        changed['request_payload_sha256'] = 'changed'
        capture.write_text(json.dumps(changed), encoding='utf-8')
        self.assertIsNone(view.recover_embedding('recover_identity'))
        capture.write_text(original, encoding='utf-8')
        with closing(sqlite3.connect(provider.ledger.path)) as ledger, ledger:
            ledger.execute("UPDATE calls SET status='UNKNOWN'")
        self.assertEqual(view.recover_embedding('recover_identity')['physical_status'], 'UNKNOWN')
        dispose()
        view.models.register('embedding', EmbeddingAdapter(provider))
        with self.assertRaisesRegex(EmbeddingCallError, 'MODEL_BINDING_CHANGED'):
            view.recover_embedding('recover_identity')
        self.assertEqual(len(self.sent), 1)

    def test_embedding_view_preserves_known_unsent_connection_classification(self):
        for transport_error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            with self.subTest(error=transport_error.__name__):
                def respond(request):
                    raise transport_error('synthetic connection failure', request=request)
                adapter = self.adapter(transport_error.__name__, respond)
                view, _ = self.view(adapter)
                with self.assertRaises(EmbeddingCallError) as raised:
                    view.documents(['one'], request_id='logical-request')
                self.assertIs(raised.exception.model_sent, False)
                self.assertTrue(raised.exception.physical_request_id)
                self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 0})
                self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 0)

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

    def test_plain_google_texts_only_adapter_stays_supported(self):
        observed = self.observed()
        plain = observed.remote
        plain_view, _ = self.view(EmbeddingAdapter(plain), dimension=768)
        self.assertEqual(len(plain_view.query('query')), 768)
        self.assertEqual(len(plain_view.documents(['source text'])), 1)
        self.assertEqual(len(self.sent), 2)


def live_contract_suite(dsn):
    class LiveModelEmbeddingReviewContracts(ModelFixtures, unittest.TestCase):
        def setUp(self):
            super().setUp()
            self.reset_jobs()

        def reset_jobs(self):
            self.scope = ('model-r3-' + uuid4().hex, 'kb', 'build')
            self.jobs = EmbeddingJobs(dsn, lease_seconds=1, max_attempts=1, circuit_threshold=1)

        def audit(self):
            return {**super().audit(), 'tenant_id': self.scope[0], 'kb_id': self.scope[1], 'build_id': self.scope[2]}

        def run_jobs(self, view):
            return self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: True, self.audit())

        def expire(self):
            with self.jobs._tx(*self.scope[:2]) as connection:
                connection.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' "
                    'WHERE tenant_id=%s AND kb_id=%s AND build_id=%s', self.scope)

        def check_retry(self, view):
            row = self.jobs.status(*self.scope)[0]
            self.assertEqual((row['status'], row['attempt']), ('RETRY', 0))
            with self.jobs._tx(*self.scope[:2]) as connection:
                circuit = connection.execute('SELECT failures,open_until FROM embedding_circuits '
                    'WHERE tenant_id=%s AND kb_id=%s', self.scope[:2]).fetchone()
                attempts = connection.execute('SELECT status FROM embedding_attempts '
                    'WHERE tenant_id=%s AND kb_id=%s', self.scope[:2]).fetchall()
            self.assertEqual(circuit, {'failures': 0, 'open_until': None})
            self.assertTrue(attempts)
            self.assertEqual({attempt['status'] for attempt in attempts}, {'ERROR'})
            self.assertEqual(len(self.run_jobs(view)), 1)
            final = self.jobs.status(*self.scope)[0]
            self.assertEqual((final['status'], final['attempt']), ('COMPLETE', 1))
            self.assertNotEqual(row['request_id'], final['request_id'])

        def test_real_http_adapter_known_unsent_errors_retry_with_new_request_ids(self):
            for transport_error in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                with self.subTest(error=transport_error.__name__):
                    self.reset_jobs()
                    failing = [True]
                    def respond(request):
                        if failing[0]:
                            raise transport_error('synthetic connection failure', request=request)
                        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})
                    adapter = self.adapter(transport_error.__name__, respond)
                    view, _ = self.view(adapter)
                    for _ in range(2):
                        with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
                            self.run_jobs(view)
                        self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 0})
                        self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 0)
                    failing[0] = False
                    self.check_retry(view)
                    self.assertEqual(adapter.ledger.summary()['physical_requests'], 3)
                    self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 1})

        def test_real_http_adapter_late_unsent_recovers_before_and_after_reconcile(self):
            for reconcile_first in (False, True):
                with self.subTest(reconcile_first=reconcile_first):
                    self.reset_jobs()
                    failing = [True]
                    def respond(request):
                        if failing[0]:
                            self.expire()
                            if reconcile_first:
                                self.assertEqual(self.jobs.reconcile(*self.scope, view, lambda: True)[0]['status'],
                                                 'UNKNOWN')
                            raise httpx.ConnectTimeout('synthetic late connection failure', request=request)
                        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})
                    adapter = self.adapter('late-' + str(reconcile_first), respond)
                    view, _ = self.view(adapter)
                    with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
                        self.run_jobs(view)
                    self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 0})
                    self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 0)
                    failing[0] = False
                    self.check_retry(view)

        def test_real_http_adapter_sent_timeout_remains_unknown_after_expiration(self):
            for expired in (False, True):
                with self.subTest(expired=expired):
                    self.reset_jobs()
                    before = len(self.sent)
                    def respond(request):
                        if expired:
                            self.expire()
                        raise httpx.ReadTimeout('synthetic sent outcome unknown', request=request)
                    adapter = self.adapter('sent-' + str(expired), respond)
                    view, _ = self.view(adapter)
                    with self.assertRaisesRegex(EmbeddingJobError,
                            'EMBEDDING_LEASE_LOST' if expired else 'EMBEDDING_UNKNOWN'):
                        self.run_jobs(view)
                    row = self.jobs.reconcile(*self.scope, view, lambda: True)[0]
                    self.assertEqual((row['status'], row['attempt']), ('UNKNOWN', 1))
                    with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_UNKNOWN'):
                        self.run_jobs(view)
                    self.assertEqual(adapter.ledger.summary()['pending_or_unknown'], 1)
                    self.assertEqual(adapter.ledger.summary()['effective_usage_or_reserved'], {'requests': 1})
                    self.assertEqual(len(self.sent) - before, 1)

        def test_standard_google_chain_recovers_expired_unsent_capture_without_resending(self):
            provider = self.observed()
            view, _ = self.view(EmbeddingAdapter(provider), dimension=768)
            reserve = provider.ledger.reserve
            allowed = [True]
            def revoke(*args, **kwargs):
                reserve(*args, **kwargs)
                self.expire()
                allowed[0] = False
            with patch.object(provider.ledger, 'reserve', side_effect=revoke):
                with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEASE_LOST'):
                    self.jobs.run(*self.scope, view, [{'text': 'one'}], lambda: allowed[0], self.audit())
            first = self.jobs.status(*self.scope)[0]
            self.assertEqual(self.sent, [])
            capture = view.recover_embedding(first['request_id'])
            self.assertEqual(capture['physical_status'], 'ERROR')
            self.assertEqual(capture['record']['physical_embedding_attempts'], 0)
            self.assertEqual(capture['physical_usage'], {'remote_embedding_calls': 0, 'input_tokens': 0})
            allowed[0] = True
            recovered = self.jobs.reconcile(*self.scope, view, lambda: allowed[0])[0]
            self.assertEqual((recovered['status'], recovered['attempt']), ('RETRY', 0))
            self.check_retry(view)
            self.assertEqual(len(self.sent), 1)

    return unittest.defaultTestLoader.loadTestsFromTestCase(LiveModelEmbeddingReviewContracts)

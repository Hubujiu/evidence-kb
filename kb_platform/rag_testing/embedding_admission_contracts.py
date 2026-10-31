from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from threading import Event, Thread, current_thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.embedding_jobs import EmbeddingJobError, EmbeddingJobs, classify_provider_failure
from kb_platform.embeddings import EmbeddingCallError
from kb_platform.model_adapters import EmbeddingAdapter
from kb_platform.model_configuration import EmbeddingView
from kb_platform.model_service import ModelService

from .budget import BudgetLedger
from .embedding_jobs_contracts import google_audit, google_preflight_fixture


class EmbeddingAdmissionContracts(unittest.TestCase):
    def check_pending_admission(self, standard_chain):
        with google_preflight_fixture() as provider, closing(ModelService()) as models:
            provider.ledger = BudgetLedger(provider.ledger.path.with_name('concurrent.sqlite'),
                {'remote_embedding_calls': 2, 'input_tokens': 16384})
            provider.remote.platform, provider.remote._headers, provider.remote._issued_requests = 'gemini_api', {}, 0
            provider.remote._validated_vector = lambda values: values
            if standard_chain:
                models.register('embedding', EmbeddingAdapter(provider))
                invoked = EmbeddingView(models, dimension=2)
            else:
                invoked = provider
            checked, release_a, sending_b, release_b = Event(), Event(), Event(), Event()
            results, errors, sends = {}, {}, []
            summary = provider.ledger.summary

            def gated_summary():
                result = summary()
                if current_thread().name == 'admission-A':
                    checked.set()
                    if not release_a.wait(5):
                        raise RuntimeError('A was not released')
                return result

            def respond(path, payload, headers, purpose, key):
                label = payload['content']['parts'][0]['text']
                sends.append(label)
                provider.remote._issued_requests += 1
                sending_b.set()
                if not release_b.wait(5):
                    raise RuntimeError('B was not released')
                return {'embedding': {'values': [1., 0.]}, 'usageMetadata': {'promptTokenCount': 1}}

            def invoke(label):
                try:
                    results[label] = invoked.documents([label], request_id=label,
                        audit_context=google_audit(), authorize=lambda: True)
                except Exception as error:
                    errors[label] = error

            with patch.object(provider.ledger, 'summary', side_effect=gated_summary), \
                    patch.object(provider.remote, '_post', side_effect=respond):
                caller_a = Thread(target=invoke, args=('A',), name='admission-A')
                caller_b = Thread(target=invoke, args=('B',), name='admission-B')
                caller_a.start()
                try:
                    self.assertTrue(checked.wait(5))
                    caller_b.start()
                    self.assertTrue(sending_b.wait(5))
                    release_a.set()
                    caller_a.join(5)
                    self.assertFalse(caller_a.is_alive())
                    with closing(sqlite3.connect(provider.ledger.path)) as ledger:
                        self.assertEqual(ledger.execute('SELECT id,status FROM calls').fetchall(), [('B', 'STARTED')])
                finally:
                    release_a.set()
                    release_b.set()
                    caller_a.join(5)
                    if caller_b.ident is not None:
                        caller_b.join(5)
            self.assertFalse(caller_b.is_alive())
            self.assertEqual(set(errors), {'A'})
            self.assertEqual(results['B'], [[1., 0.]])
            self.assertEqual(sends, ['B'])
            self.assertFalse((Path(provider.audit_dir) / 'A.json').exists())
            self.assertEqual(summary()['pending_or_unknown'], 0)
            with closing(sqlite3.connect(provider.ledger.path)) as ledger:
                self.assertEqual(ledger.execute('SELECT id,status FROM calls').fetchall(), [('B', 'COMPLETE')])
            row = dict(tenant_id='t', kb_id='k', build_id='b', batch_no=0, request_id='A',
                       attempt=1, provider_hash='p')
            error = classify_provider_failure(row, invoked, errors['A'])
            statements = []

            class Port:
                def execute(self, query, parameters):
                    statements.append((query, parameters))
                    return SimpleNamespace(fetchone=lambda: {'retry_at': None})

            @contextmanager
            def transaction(*scope):
                yield Port()

            jobs = EmbeddingJobs('recording-sql')
            with patch.object(jobs, '_tx', side_effect=transaction):
                with self.assertRaises(EmbeddingJobError) as finished:
                    jobs._finish(row, error=error)
            update, parameters = statements[0]
            self.assertEqual(parameters[0], 'RETRY')
            self.assertIn('attempt=attempt-CASE WHEN %s AND attempt>0 THEN 1 ELSE 0 END', update)
            self.assertIs(parameters[4], True)
            self.assertFalse(any('failures=failures+1' in query for query, _ in statements))
            self.assertEqual(finished.exception.code, 'EMBEDDING_LEDGER_BLOCKED')
            self.assertIsInstance(errors['A'], EmbeddingCallError)
            self.assertEqual(errors['A'].preflight_code, 'UNRESOLVED_PHYSICAL_REQUEST')

    def test_direct_google_atomic_admission_retries_unsent_job(self):
        self.check_pending_admission(False)

    def test_standard_google_atomic_admission_retries_unsent_job(self):
        self.check_pending_admission(True)

    def test_existing_capture_or_physical_record_cannot_prove_unsent(self):
        for evidence in ('capture', 'physical'):
            with self.subTest(evidence=evidence), google_preflight_fixture() as provider:
                if evidence == 'capture':
                    provider.audit_dir.mkdir()
                    (provider.audit_dir / 'A.json').write_text('{}', encoding='utf-8')
                else:
                    provider.ledger.reserve('A', {'remote_embedding_calls': 1, 'input_tokens': 8192})
                self.assertIsNone(provider.embedding_preflight('A', ValueError('UNRESOLVED_PHYSICAL_REQUEST')))
                self.assertEqual(provider.remote.calls, 0)

    def test_existing_physical_identity_is_not_reclassified_at_reserve_boundary(self):
        for standard_chain in (False, True):
            with self.subTest(standard_chain=standard_chain), google_preflight_fixture() as provider, \
                    closing(ModelService()) as models:
                provider.ledger.reserve('A', {'remote_embedding_calls': 1, 'input_tokens': 8192})
                if standard_chain:
                    models.register('embedding', EmbeddingAdapter(provider))
                    invoked = EmbeddingView(models, dimension=2)
                else:
                    invoked = provider
                with patch.object(provider.ledger, 'summary', return_value={'pending_or_unknown': 0}):
                    with self.assertRaises((ValueError, EmbeddingCallError)) as raised:
                        invoked.documents(['A'], request_id='A', audit_context=google_audit(), authorize=lambda: True)
                error = raised.exception
                self.assertIsNone(getattr(error, 'preflight_code', None))
                self.assertIsNone(getattr(error, 'model_sent', None))
                self.assertIs(classify_provider_failure({'request_id': 'A'}, invoked, error), error)
                self.assertEqual(provider.remote.calls, 0)
                self.assertEqual(provider.ledger.summary()['pending_or_unknown'], 1)

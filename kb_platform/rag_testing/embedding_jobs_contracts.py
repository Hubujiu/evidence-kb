import json
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.embedding_jobs import (EmbeddingJobError, EmbeddingJobs, digest,
                                        recovered_vectors, retry_delay, validate_vectors,
                                        EmbeddingPreflightError, classify_provider_failure)
from kb_platform.rag_testing.contracts import digest as semantic_digest


@contextmanager
def captured_google_fixture(platform):
    import httpx
    from kb_platform.remote import GoogleEmbeddingProvider
    from kb_platform.rag_testing.budget import BudgetLedger
    from kb_platform.rag_testing.provider_capture import observe_client

    with TemporaryDirectory() as directory:
        root = Path(directory)
        config = {'google': {'platform': platform, 'model': 'gemini-embedding-2',
            'base_url': 'https://fixture.invalid/v1', 'api_key': 'synthetic', 'dimension': 768,
            'project_id': 'fixture-project', 'location': 'us', 'service_account_file': 'unused',
            'cache_mode': 'disabled', 'retries': 0, 'workers': 1},
            'cache_dir': str(root / 'cache'), 'log_path': str(root / 'calls.jsonl')}
        path = root / 'providers.json'
        path.write_text(json.dumps(config), 'utf-8')
        sent = []

        def respond(request):
            payload = json.loads(request.content)
            sent.append(payload)
            vector = [1.] + [0.] * 767
            body = ({'embeddings': [{'values': vector} for _ in payload['requests']]}
                    if 'requests' in payload else {'embedding': {'values': vector}})
            return httpx.Response(200, json={**body, 'usageMetadata': {'promptTokenCount': 1}})

        with patch.object(GoogleEmbeddingProvider, '_init_vertex_auth'), \
                patch.object(GoogleEmbeddingProvider, '_vertex_headers', return_value={}):
            provider = GoogleEmbeddingProvider(path)
            caps = dict(model=0, input_tokens=0, output_tokens=0,
                        remote_embedding_calls=1, embedding_input_tokens=50000)
            ledger = BudgetLedger(root / 'physical.sqlite', caps)
            observe_client(provider, ledger, root / 'capture', category='synthetic', inner=httpx.MockTransport(respond))
            try:
                yield SimpleNamespace(provider=provider, ledger=ledger, root=root, sent=sent, respond=respond)
            finally:
                provider.close()


@contextmanager
def google_preflight_fixture():
    from kb_platform.google_semantic_provider import GoogleSemanticProvider
    from kb_platform.rag_testing.budget import BudgetLedger

    class Transport:
        dimension = 2
        model = 'gemini-embedding-2'
        manifest = dict(provider='google_gemini', normalization='l2', dimension=2,
                        model=model, max_input_characters=10000, document_format='fixture', query_format='fixture')

        def __init__(self, config):
            self.calls = 0

        def _request_item(self, text, task):
            return {'content': {'parts': [{'text': text}]}}

        def _post(self, *args):
            self.calls += 1
            raise AssertionError('Preflight must not reach the network')

        def close(self):
            pass

    with TemporaryDirectory() as directory:
        ledger = BudgetLedger(Path(directory) / 'ledger.sqlite', {'remote_embedding_calls': 1, 'input_tokens': 8192})
        provider = GoogleSemanticProvider('unused', ledger=ledger, audit_dir=Path(directory) / 'audit', remote_factory=Transport)
        try:
            yield provider
        finally:
            provider.close()


def google_audit():
    return dict(tenant_id='t', kb_id='k', build_id='b', principal='owner', acl_revision='1',
                sources=[dict(source_id='s', source_version_id='v', parse_snapshot_id='p', source_text_sha256='h')])


def google_admission_race(provider, first_call):
    from .budget import BudgetLedger

    checked, admitted, release_first, complete_second = Event(), Event(), Event(), Event()
    outcomes, sent = {}, []
    provider.ledger = BudgetLedger(provider.ledger.path.with_name('concurrent.sqlite'),
        {'remote_embedding_calls': 2, 'input_tokens': 16384})
    provider.remote.platform, provider.remote._headers, provider.remote._issued_requests = 'gemini_api', {}, 0
    provider.remote._validated_vector = lambda values: values
    summary = provider.ledger.summary

    def gated_summary():
        snapshot = summary()
        if current_thread().name == 'request-A':
            checked.set()
            if not release_first.wait(5):
                raise RuntimeError('request A was not released')
        return snapshot

    def respond(path, payload, headers, purpose, key):
        sent.append(payload['content']['parts'][0]['text'])
        provider.remote.calls += 1
        provider.remote._issued_requests += 1
        admitted.set()
        if not complete_second.wait(5):
            raise RuntimeError('request B was not released')
        return {'embedding': {'values': [1., 0.]}, 'usageMetadata': {'promptTokenCount': 1}}

    def invoke(request_id):
        try:
            outcomes[request_id] = first_call() if request_id == 'A' else provider.documents(['B'],
                request_id='B', audit_context=google_audit(), authorize=lambda: True)
        except Exception as error:
            outcomes[request_id] = error

    provider.remote._post = respond
    first = Thread(target=invoke, args=('A',), name='request-A')
    second = Thread(target=invoke, args=('B',), name='request-B')
    with patch.object(provider.ledger, 'summary', side_effect=gated_summary):
        first.start()
        try:
            if not checked.wait(3):
                raise RuntimeError('request A did not read the idle ledger')
            second.start()
            if not admitted.wait(3) or summary()['pending_or_unknown'] != 1:
                raise RuntimeError('request B did not enter STARTED')
            release_first.set()
            first.join(3)
            if first.is_alive():
                raise RuntimeError('request A did not finish while request B remained STARTED')
        finally:
            release_first.set()
            complete_second.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
    if first.is_alive() or second.is_alive():
        raise RuntimeError('admission contract workers did not terminate')
    return outcomes, sent


class EmbeddingJobsContracts(unittest.TestCase):
    def test_captured_google_refusal_preserves_zero_send_classification(self):
        for platform in ('gemini_api', 'vertex_ai'):
            for scenario, expected in [('budget', 'EMBEDDING_BUDGET_INSUFFICIENT'),
                                       ('pending', 'EMBEDDING_LEDGER_BLOCKED')]:
                with self.subTest(platform=platform, scenario=scenario), captured_google_fixture(platform) as f:
                    resources = dict.fromkeys(f.ledger.caps, 0)
                    resources['remote_embedding_calls'] = 1
                    f.ledger.reserve('prior', resources)
                    if scenario == 'budget':
                        f.ledger.finish('prior', status='COMPLETE', usage=resources)
                    before = f.ledger.summary()
                    with self.assertRaises(Exception) as raised:
                        f.provider.documents(['synthetic source'])
                    error = classify_provider_failure({}, f.provider, raised.exception)
                    self.assertIsInstance(error, EmbeddingPreflightError)
                    self.assertEqual(error.code, expected)
                    self.assertEqual(f.sent, [])
                    self.assertEqual(f.ledger.summary(), before)

    def test_error_text_and_general_admission_failure_do_not_prove_zero_send(self):
        from kb_platform.provider_admission import CaptureAdmissionError

        for error in (RuntimeError('BUDGET_INSUFFICIENT'), CaptureAdmissionError('BUDGET_INSUFFICIENT'),
                      TimeoutError('UNRESOLVED_PHYSICAL_REQUEST')):
            self.assertIs(classify_provider_failure({}, object(), error), error)

    def test_real_google_preflight_errors_are_known_without_network(self):
        for scenario, expected in [('input', 'EMBEDDING_INPUT_TOO_LARGE'),
                                   ('budget', 'EMBEDDING_BUDGET_INSUFFICIENT'),
                                   ('pending', 'EMBEDDING_LEDGER_BLOCKED')]:
            with self.subTest(scenario=scenario), google_preflight_fixture() as provider:
                if scenario != 'input':
                    provider.ledger.reserve('prior', {'remote_embedding_calls': 1, 'input_tokens': 8192})
                    if scenario == 'budget':
                        provider.ledger.finish('prior', status='COMPLETE', usage={'remote_embedding_calls': 1, 'input_tokens': 8192})
                before = provider.ledger.summary()
                try:
                    provider.documents(['x' * 8193 if scenario == 'input' else 'text'], request_id='fresh',
                                       audit_context=google_audit(), authorize=lambda: True)
                except Exception as error:
                    original = error
                    classified = classify_provider_failure({'request_id': 'fresh'}, provider, error)
                else:
                    self.fail('Expected a preflight refusal')
                self.assertIsInstance(classified, EmbeddingPreflightError)
                self.assertEqual(classified.code, expected)
                self.assertEqual(provider.remote.calls, 0)
                self.assertEqual(provider.ledger.summary(), before)
                self.assertIsInstance(classify_provider_failure({'request_id': 'fresh'}, provider, TimeoutError()), TimeoutError)
                if scenario == 'input':
                    provider.ledger.reserve('fresh', {'remote_embedding_calls': 1, 'input_tokens': 8192})
                    self.assertIs(classify_provider_failure({'request_id': 'fresh'}, provider, original), original)

    def test_untyped_budget_diagnostic_does_not_prove_request_was_unsent(self):
        from kb_platform.embeddings import EmbeddingCallError
        from kb_platform.model_errors import RemoteModelError

        for original in (ValueError('BUDGET_INSUFFICIENT'), ValueError('UNRESOLVED_PHYSICAL_REQUEST'),
                         RemoteModelError('PROVIDER_OUTCOME_UNKNOWN')):
            error = EmbeddingCallError(original)
            self.assertIs(classify_provider_failure({'request_id': 'fresh'}, object(), error), error)

    def test_ambiguous_transport_never_retries(self):
        for error in (TimeoutError(), ConnectionError(), RuntimeError()):
            self.assertIsNone(retry_delay(error, 1))

    def test_retry_after_and_terminal_errors(self):
        error = RuntimeError()
        error.status, error.retry_after_seconds = 429, 120
        self.assertEqual(retry_delay(error, 1), 120)
        error.status = 401
        self.assertIsNone(retry_delay(error, 1))

    def test_vector_shape_nan_zero_are_rejected(self):
        for vectors in ([[1]], [[float('nan'), 1]], [[0, 0]], [[True, 1]], []):
            with self.assertRaises(EmbeddingJobError):
                validate_vectors(vectors, 1, 2)
        self.assertEqual(validate_vectors([[1., 0.]], 1, 2), [[1., 0.]])

    def test_authorization_is_explicit(self):
        for callback in (None, lambda: None, lambda: False):
            with self.assertRaises(EmbeddingJobError):
                EmbeddingJobs._auth(callback)
        EmbeddingJobs._auth(lambda: True)

    def test_dependencies_include_order_and_identity(self):
        first = {'units': [{'text': 'a', 'source_id': 's1'}, {'text': 'b', 'source_id': 's2'}]}
        self.assertNotEqual(digest(first), digest({'units': list(reversed(first['units']))}))
        self.assertNotEqual(digest(first), digest({'units': [{'text': 'a', 'source_id': 's3'}]}))

    def test_recovery_requires_capture_and_settled_matching_ledger(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            ledger = folder / 'ledger.sqlite'
            with closing(sqlite3.connect(ledger)) as conn, conn:
                conn.execute('CREATE TABLE calls (id TEXT, status TEXT, usage TEXT)')
                conn.execute("INSERT INTO calls VALUES ('r','UNKNOWN',?)", (json.dumps({'calls': 1}),))
            provider = SimpleNamespace(audit_dir=folder, ledger=SimpleNamespace(path=ledger),
                                       manifest={'model': 'frozen'}, dimension=2)
            row = {'request_id': 'r', 'payload': {'units': [{'text': 'hello'}],
                   'manifest': provider.manifest, 'audit_context': {'acl': 2}}}
            record = {'status': 'COMPLETE', 'task': 'document', 'request_id': 'r',
                      'physical_request_id': 'r', 'texts': ['hello'], 'provider_manifest': provider.manifest,
                      'audit_context': {'acl': 2}, 'vectors': [[1., 0.]], 'usage': {'calls': 1},
                      'vectors_sha256': semantic_digest([[1., 0.]])}
            (folder / 'r.json').write_text(json.dumps(record), 'utf-8')
            self.assertIsNone(recovered_vectors(row, provider))
            with closing(sqlite3.connect(ledger)) as conn, conn:
                conn.execute("UPDATE calls SET status='COMPLETE'")
            self.assertEqual(recovered_vectors(row, provider), [[1., 0.]])
            record['texts'] = ['different']
            (folder / 'r.json').write_text(json.dumps(record), 'utf-8')
            self.assertIsNone(recovered_vectors(row, provider))


def live_contract_suite(dsn):

    from uuid import uuid4

    class LiveEmbeddingJobsContracts(unittest.TestCase):
        def setUp(self):
            self.scope = ('jobs-contract-' + uuid4().hex, 'kb', 'build')
            self.jobs = EmbeddingJobs(dsn)
            self.units = [{'text': 'one', 'source_id': 'source'}, {'text': 'two', 'source_id': 'source'}]
            self.audit = {'principal': 'owner', 'acl_revision': '0'}
            self.calls = []
            outer = self

            class Provider:
                manifest = {'provider': 'offline-contract', 'model': 'fixture-v1', 'dimension': 2}
                dimension = 2
                max_batch_size = 1
                error_on = None

                def documents(self, texts):
                    outer.calls.append(texts)
                    if self.error_on == texts[0]:
                        error = RuntimeError('fixture 503')
                        error.status = 503
                        raise error
                    return [[1., 0.] for _ in texts]

            self.provider = Provider()

        def run_jobs(self, *, units=None, build=None):
            return self.jobs.run(*self.scope[:2], build or self.scope[2], self.provider,
                                 units if units is not None else self.units, lambda: True, self.audit)

        def test_captured_google_budget_resume_reuses_completed_batches(self):
            import httpx
            from kb_platform.rag_testing.budget_batches import LinkedBudgetLedger, freeze_predecessor
            from kb_platform.rag_testing.provider_capture import observe_client

            for platform in ('gemini_api', 'vertex_ai'):
                with self.subTest(platform=platform), captured_google_fixture(platform) as f:
                    scope = (*self.scope[:2], platform)
                    units = [{'text': f'synthetic {i}'} for i in range(17 if platform == 'gemini_api' else 2)]
                    for _ in range(2):
                        with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_BUDGET_INSUFFICIENT'):
                            self.jobs.run(*scope, f.provider, units, lambda: True, self.audit)
                    rows = self.jobs.status(*scope)
                    self.assertEqual([(r['status'], r['attempt']) for r in rows], [('COMPLETE', 1), ('RETRY', 0)])
                    self.assertEqual(len(f.sent), 1)
                    with self.jobs._tx(*scope[:2]) as conn:
                        self.assertEqual(conn.execute('SELECT max(failures) n FROM embedding_circuits').fetchone()['n'], 0)
                    before = freeze_predecessor(f.ledger.path)
                    next_ledger = LinkedBudgetLedger(f.root / 'next.sqlite', f.ledger.caps, predecessors=[before])
                    observe_client(f.provider, next_ledger, f.root / 'next-capture', category='synthetic-resume',
                                   inner=httpx.MockTransport(f.respond))
                    restarted = EmbeddingJobs(dsn)
                    result = restarted.run(*scope, f.provider, units, lambda: True, self.audit)
                    self.assertEqual(len(result), len(units))
                    self.assertEqual(len(f.sent), 2)
                    self.assertEqual([r['status'] for r in restarted.status(*scope)], ['COMPLETE', 'COMPLETE'])
                    self.assertNotEqual(rows[-1]['request_id'], restarted.status(*scope)[-1]['request_id'])
                    self.assertEqual(freeze_predecessor(f.ledger.path)['snapshot_sha256'], before['snapshot_sha256'])
                    self.assertEqual(next_ledger.cumulative_summary()['cumulative']['pending_or_unknown'], 0)

        def test_google_concurrent_admission_retries_unsent_job_after_competitor_completes(self):
            with google_preflight_fixture() as provider:
                outcomes, sent = google_admission_race(provider, lambda: self.jobs.run(*self.scope,
                    provider, [{'text': 'A'}], lambda: True, google_audit()))
                self.assertIsInstance(outcomes['A'], EmbeddingJobError)
                self.assertEqual(outcomes['A'].code, 'EMBEDDING_LEDGER_BLOCKED')
                self.assertEqual(outcomes['B'], [[1., 0.]])
                self.assertEqual(sent, ['B'])
                self.assertEqual(provider.remote.calls, 1)
                self.assertEqual(provider.ledger.summary()['pending_or_unknown'], 0)
                row = self.jobs.status(*self.scope)[0]
                self.assertEqual((row['status'], row['attempt'], row['error']),
                                 ('RETRY', 0, 'EMBEDDING_LEDGER_BLOCKED'))
                self.assertFalse((provider.audit_dir / (row['request_id'] + '.json')).exists())
                with closing(sqlite3.connect(provider.ledger.path)) as database:
                    self.assertEqual(database.execute('SELECT id,status FROM calls').fetchall(), [('B', 'COMPLETE')])
                with self.jobs._tx(*self.scope[:2]) as conn:
                    self.assertEqual(conn.execute('SELECT failures FROM embedding_circuits').fetchone()['failures'], 0)
                    attempt = conn.execute('SELECT status,diagnostic FROM embedding_attempts').fetchone()
                    self.assertEqual((attempt['status'], attempt['diagnostic']), ('ERROR', 'EMBEDDING_LEDGER_BLOCKED'))
                self.assertEqual(self.jobs.reconcile(*self.scope, provider, lambda: True)[0]['status'], 'RETRY')
                self.assertEqual(len(self.jobs.run(*self.scope, provider, [{'text': 'A'}], lambda: True, google_audit())), 1)
                retried = self.jobs.status(*self.scope)[0]
                self.assertEqual((retried['status'], retried['attempt']), ('COMPLETE', 1))
                self.assertNotEqual(retried['request_id'], row['request_id'])
                self.assertEqual(sent, ['B', 'A'])

        def test_late_unsent_outcome_recovers_same_request_before_and_after_reconcile(self):
            for reconcile_first in (False, True):
                with self.subTest(reconcile_first=reconcile_first):
                    self.scope = ('late-unsent-' + uuid4().hex, 'kb', 'build')
                    self.jobs = EmbeddingJobs(dsn, lease_seconds=1, max_attempts=1)
                    original = self.provider.documents

                    def unsent(texts):
                        with self.jobs._tx(*self.scope[:2]) as conn:
                            conn.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
                        if reconcile_first:
                            self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'],
                                             'UNKNOWN')
                        error = RuntimeError('known unsent after lease expiration')
                        error.model_sent = False
                        raise error

                    self.provider.documents = unsent
                    with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
                        self.run_jobs(units=[self.units[0]])
                    row = self.jobs.status(*self.scope)[0]
                    self.assertEqual((row['status'], row['attempt']), ('RETRY', 0))
                    with self.jobs._tx(*self.scope[:2]) as conn:
                        self.assertEqual(conn.execute('SELECT failures FROM embedding_circuits').fetchone()['failures'], 0)
                        self.assertEqual(conn.execute('SELECT status FROM embedding_attempts').fetchone()['status'], 'ERROR')
                    self.provider.documents = original
                    self.assertEqual(len(self.run_jobs(units=[self.units[0]])), 1)
                    final = self.jobs.status(*self.scope)[0]
                    self.assertEqual((final['status'], final['attempt']), ('COMPLETE', 1))
                    self.assertNotEqual(row['request_id'], final['request_id'])

        def test_late_old_request_cannot_rewrite_new_claim_or_ambiguous_outcome(self):
            _, dependency = self.jobs._prepare(*self.scope, self.provider, [self.units[0]], self.audit)
            old = self.jobs._claim(*self.scope, 0, dependency)
            with self.jobs._tx(*self.scope[:2]) as conn:
                conn.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
            with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEASE_LOST'):
                self.jobs._finish(old, error=TimeoutError('sent outcome unknown'))
            self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
            error = RuntimeError('known unsent')
            error.model_sent = False
            with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
                self.jobs._finish(old, error=error)
            current = self.jobs._claim(*self.scope, 0, dependency)
            with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEASE_LOST'):
                self.jobs._finish(old, error=error)
            final = self.jobs.status(*self.scope)[0]
            self.assertEqual((final['request_id'], final['status'], final['attempt']),
                             (current['request_id'], 'RUNNING', 1))

        def test_reconcile_known_unsent_capture_requires_matching_settled_zero_usage(self):
            with TemporaryDirectory() as directory:
                self.provider.audit_dir = Path(directory)
                ledger = Path(directory) / 'physical.sqlite'
                self.provider.ledger = SimpleNamespace(path=ledger)
                _, dependency = self.jobs._prepare(*self.scope, self.provider, [self.units[0]], self.audit)
                row = self.jobs._claim(*self.scope, 0, dependency)
                record = {'status': 'ERROR', 'request_id': row['request_id'],
                    'physical_request_id': row['request_id'], 'task': 'document', 'texts': ['one'],
                    'provider_manifest': self.provider.manifest, 'audit_context': self.audit,
                    'reserved': {'calls': 1}, 'usage': {'calls': 0}, 'physical_embedding_attempts': 0}
                target = Path(directory) / (row['request_id'] + '.json')
                with closing(sqlite3.connect(ledger)) as conn, conn:
                    conn.execute('CREATE TABLE calls(id TEXT,status TEXT,usage TEXT)')
                    conn.execute('INSERT INTO calls VALUES (?,?,?)',
                                 (row['request_id'], 'UNKNOWN', json.dumps({'calls': 0})))
                with self.jobs._tx(*self.scope[:2]) as conn:
                    conn.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
                target.write_text(json.dumps(record), 'utf-8')
                self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
                with closing(sqlite3.connect(ledger)) as conn, conn:
                    conn.execute("UPDATE calls SET status='ERROR'")
                for change in ({'request_id': 'other'}, {'audit_context': {}}, {'texts': ['other']},
                               {'physical_embedding_attempts': 1}, {'usage': {'calls': 1}}):
                    target.write_text(json.dumps({**record, **change}), 'utf-8')
                    self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
                target.write_text(json.dumps(record), 'utf-8')
                final = self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]
                self.assertEqual((final['status'], final['attempt']), ('RETRY', 0))
                self.assertEqual(len(self.run_jobs(units=[self.units[0]])), 1)

        def test_partial_restart_reuses_only_completed_batches(self):
            self.provider.error_on = 'two'
            with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
                self.run_jobs()
            self.assertEqual([r['status'] for r in self.jobs.status(*self.scope)], ['COMPLETE', 'RETRY'])
            with self.jobs._tx(*self.scope[:2]) as conn:
                conn.execute("UPDATE embedding_jobs SET retry_at=clock_timestamp() WHERE status='RETRY'")
            self.provider.error_on = None
            self.assertEqual(len(self.run_jobs()), 2)
            self.assertEqual(self.calls, [['one'], ['two'], ['two']])
            self.assertEqual(len(self.run_jobs()), 2)
            self.assertEqual(len(self.calls), 3)
            with self.jobs._tx(*self.scope[:2]) as conn:
                self.assertEqual(conn.execute('SELECT count(DISTINCT request_id) n FROM embedding_attempts').fetchone()['n'], 3)

        def test_google_preflight_is_terminal_diagnostic_not_unknown(self):
            with google_preflight_fixture() as provider:
                for _ in range(2):
                    with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_INPUT_TOO_LARGE'):
                        self.jobs.run(*self.scope, provider, [{'text': 'x' * 8193}], lambda: True, google_audit())
                row = self.jobs.status(*self.scope)[0]
                self.assertEqual((row['status'], row['attempt'], row['error']),
                                 ('ERROR', 1, 'EMBEDDING_INPUT_TOO_LARGE'))
                self.assertEqual(provider.remote.calls, 0)
                self.assertEqual(provider.ledger.summary()['physical_requests'], 0)

        def test_google_batching_uses_one_request_per_sixteen_and_preserves_old_batches(self):
            from kb_platform.remote import GoogleEmbeddingProvider

            provider = object.__new__(GoogleEmbeddingProvider)
            provider.platform = 'gemini_api'
            provider.model = 'gemini-embedding-2'
            provider.dimension = 2
            provider.manifest = {'model': provider.model, 'dimension': 2, 'max_input_characters': 12000}
            provider._headers = {}
            provider.document_count = 0
            requests = []
            provider._cache_get = lambda key: None
            provider._cache_put = lambda key, value: None

            def send(path, payload, headers, purpose, key, units):
                requests.append(len(payload['requests']))
                return {'embeddings': [{'values': [1., 0.]} for _ in payload['requests']]}

            provider._post = send
            units = [{'text': f'text {i}'} for i in range(33)]
            result = self.jobs.run(*self.scope, provider, units, lambda: True, self.audit)
            self.assertEqual(len(result), 33)
            self.assertEqual(requests, [16, 16, 1])
            self.assertEqual([r['status'] for r in self.jobs.status(*self.scope)], ['COMPLETE'] * 3)
            self.jobs.run(*self.scope, provider, units, lambda: True, self.audit)
            self.assertEqual(requests, [16, 16, 1])

            old_scope = (*self.scope[:2], 'old-build')
            provider.platform = 'vertex_ai'
            self.jobs._prepare(*old_scope, provider, units[:2], self.audit)
            provider.platform = 'gemini_api'
            requests.clear()
            self.jobs.run(*old_scope, provider, units[:2], lambda: True, self.audit)
            self.assertEqual(requests, [1, 1])

        def test_recoverable_google_admission_resumes_without_spending_attempt_budget(self):
            from kb_platform.embeddings import EmbeddingError

            for number, (message, code) in enumerate([
                ('BUDGET_INSUFFICIENT', 'EMBEDDING_BUDGET_INSUFFICIENT'),
                ('Unresolved physical outcome requires inspection before another embedding request', 'EMBEDDING_LEDGER_BLOCKED'),
            ]):
                scope = (*self.scope[:2], f'blocked-{number}')
                with google_preflight_fixture() as provider:
                    with patch.object(provider, 'documents', side_effect=EmbeddingError(message)):
                        for _ in range(4):
                            with self.assertRaisesRegex(EmbeddingJobError, code):
                                self.jobs.run(*scope, provider, self.units[:1], lambda: True, google_audit())
                    row = self.jobs.status(*scope)[0]
                    self.assertEqual((row['status'], row['error'], row['attempt']), ('RETRY', code, 0))

                    with self.jobs._tx(*scope[:2]) as conn:
                        conn.execute("UPDATE embedding_jobs SET status='ERROR' WHERE build_id=%s", (scope[2],))
                    with patch.object(provider, 'documents', return_value=[[1., 0.]]) as documents:
                        self.assertEqual(len(self.jobs.run(*scope, provider, self.units[:1], lambda: True, google_audit())), 1)
                        documents.assert_called_once()
                    self.assertEqual(self.jobs.status(*scope)[0]['status'], 'COMPLETE')
                    self.assertEqual(provider.remote.calls, 0)
                    self.assertEqual(provider.ledger.summary()['physical_requests'], 0)

        def test_expired_owner_is_fenced_and_unknown_never_resent(self):
            _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
            row = self.jobs._claim(*self.scope, 0, dependency)
            with self.jobs._tx(*self.scope[:2]) as conn:
                conn.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE batch_no=0")
            with self.assertRaisesRegex(EmbeddingJobError, 'LEASE_LOST'):
                self.jobs._finish(row, vectors=[[1., 0.]])
            with self.assertRaisesRegex(EmbeddingJobError, 'UNKNOWN'):
                self.run_jobs()
            self.assertEqual(self.calls, [])
            self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'UNKNOWN')
            with self.jobs._tx(*self.scope[:2]) as conn:
                self.assertEqual(conn.execute('SELECT status FROM embedding_attempts').fetchone()['status'], 'UNKNOWN')

        def test_circuit_is_durable_across_job_instances(self):
            self.jobs = EmbeddingJobs(dsn, circuit_threshold=1)
            self.provider.error_on = 'one'
            with self.assertRaisesRegex(EmbeddingJobError, 'RETRY'):
                self.run_jobs()
            self.jobs = EmbeddingJobs(dsn, circuit_threshold=1)
            with self.assertRaisesRegex(EmbeddingJobError, 'CIRCUIT_OPEN'):
                self.run_jobs(build='another-build')
            self.assertEqual(self.calls, [['one']])

        def test_retries_have_finite_attempt_limit(self):
            self.jobs = EmbeddingJobs(dsn, max_attempts=2, circuit_threshold=5)
            self.provider.error_on = 'one'
            with self.assertRaisesRegex(EmbeddingJobError, 'RETRY'):
                self.run_jobs()
            with self.jobs._tx(*self.scope[:2]) as conn:
                conn.execute("UPDATE embedding_jobs SET retry_at=clock_timestamp() WHERE status='RETRY'")
            with self.assertRaisesRegex(EmbeddingJobError, 'ERROR'):
                self.run_jobs()
            with self.assertRaisesRegex(EmbeddingJobError, 'ERROR'):
                self.run_jobs()
            self.assertEqual(len(self.calls), 2)

        def test_changed_dependencies_and_rls(self):
            self.run_jobs()
            with self.assertRaisesRegex(EmbeddingJobError, 'DEPENDENCY_CHANGED'):
                self.run_jobs(units=list(reversed(self.units)))
            self.assertEqual(self.jobs.status('other-tenant', self.scope[1], self.scope[2]), [])
            with self.jobs._tx('other-tenant', self.scope[1]) as conn:
                self.assertEqual(conn.execute('SELECT count(*) n FROM embedding_jobs').fetchone()['n'], 0)

        def test_concurrent_different_dependencies_cannot_share_build(self):
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier
            barrier = Barrier(2)

            def prepare(units):
                barrier.wait()
                try:
                    self.jobs._prepare(*self.scope, self.provider, units, self.audit)
                    return 'prepared'
                except EmbeddingJobError as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=2) as workers:
                result = list(workers.map(prepare, [self.units, list(reversed(self.units))]))
            self.assertCountEqual(result, ['prepared', 'EMBEDDING_DEPENDENCY_CHANGED'])
            self.assertEqual(self.calls, [])

        def test_reconcile_completed_capture_without_physical_retry(self):
            with TemporaryDirectory() as folder:
                self.provider.audit_dir = Path(folder)
                ledger = Path(folder) / 'ledger.sqlite'
                self.provider.ledger = SimpleNamespace(path=ledger)
                _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
                row = self.jobs._claim(*self.scope, 0, dependency)
                request_id = row['request_id']
                with closing(sqlite3.connect(ledger)) as conn, conn:
                    conn.execute('CREATE TABLE calls(id TEXT,status TEXT,usage TEXT)')
                    conn.execute("INSERT INTO calls VALUES (?,'COMPLETE',?)", (request_id, json.dumps({'calls': 1})))
                record = {'status': 'COMPLETE', 'request_id': request_id, 'physical_request_id': request_id,
                          'task': 'document', 'texts': ['one'], 'provider_manifest': self.provider.manifest,
                          'audit_context': self.audit, 'vectors': [[1., 0.]], 'usage': {'calls': 1},
                          'vectors_sha256': semantic_digest([[1., 0.]])}
                (Path(folder) / (request_id + '.json')).write_text(json.dumps(record), 'utf-8')
                with self.jobs._tx(*self.scope[:2]) as conn:
                    conn.execute("UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE batch_no=0")
                self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'COMPLETE')
                self.assertEqual(len(self.run_jobs()), 2)
                self.assertEqual(self.calls, [['two']])

    return unittest.defaultTestLoader.loadTestsFromTestCase(LiveEmbeddingJobsContracts)


if __name__ == '__main__':
    unittest.main()

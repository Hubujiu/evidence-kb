"""Offline failure/recovery contracts; DB atomicity is covered by live operations tests."""
import json
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from kb_platform.embedding_jobs import (EmbeddingJobError, EmbeddingJobs, digest,
                                        recovered_vectors, retry_delay, validate_vectors,
                                        EmbeddingPreflightError, classify_provider_failure)
from kb_platform.rag_testing.contracts import digest as semantic_digest


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


class EmbeddingJobsContracts(unittest.TestCase):
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
    """Run explicitly against an isolated DB initialized with 001 and 003 SQL."""
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

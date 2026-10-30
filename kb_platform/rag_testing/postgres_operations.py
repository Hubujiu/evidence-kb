import argparse
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from kb_platform.core import Actor, PlatformError
from kb_platform.application_composition import create_platform
from kb_platform.settings import Settings
from kb_platform.remote import RemoteModelError
from .embedding_jobs_contracts import live_contract_suite
from .runner import write_json


def build_suite(dsn, output):
    class Builds(unittest.TestCase):
        def setUp(self):
            self.calls = []
            outer = self

            class Provider:
                provider_id = 'fixture'
                manifest = {'provider': 'fixture', 'model': 'fixed', 'dimension': 3}
                dimension = 3
                max_batch_size = 1
                fail = True

                def documents(self, texts):
                    outer.calls.append(texts)
                    if self.fail and len(outer.calls) == 2:
                        raise RemoteModelError('FIXTURE_TRANSIENT', 503)
                    return [[1., 0., 0.] for _ in texts]

            self.provider = Provider()
            self.platform = create_platform(Settings(dsn, '', output / 'originals', output / 'unused-identities', 'postgres'),
                                            embedding_provider=self.provider)
            self.addCleanup(self.platform.close)
            self.actor = Actor('pg-build-contract-' + uuid4().hex, 'owner')
            self.kb = self.platform.create_kb(self.actor, 'Recovery fixture')['kb_id']
            self.source = self.platform.ingest(self.actor, self.kb, 'one.md', b'# Policy\n\nALPHA backup.\n\nBETA restore.',
                policy={'embedding': True, 'allowed_providers': ['fixture']})

        def latest(self):
            with self.platform._tx(self.actor, self.kb) as conn:
                return conn.execute('SELECT * FROM builds').fetchone()

        def test_partial_embedding_resume_and_publish(self):
            with self.assertRaises(PlatformError) as raised:
                self.platform.stage(self.actor, self.kb, ['hybrid'])
            self.assertEqual('EMBEDDING_RETRY', raised.exception.code)
            build = self.latest()
            self.assertEqual('STAGING', build['status'])
            with self.platform._tx(self.actor, self.kb) as conn:
                conn.execute("UPDATE embedding_jobs SET retry_at=clock_timestamp() WHERE status='RETRY'")
            self.provider.fail = False
            self.platform.resume_build(self.actor, self.kb, build['build_id'])
            self.assertEqual(1, self.calls.count(self.calls[0]))
            self.platform.publish(self.actor, self.kb, build['build_id'])
            self.assertTrue(self.platform.search(self.actor, self.kb, 'ALPHA')['hits'])
            self.assertTrue(all(r['status'] == 'COMPLETE' for r in self.platform.build_status(self.actor,self.kb,build['build_id'])['embedding_batches']))

        def test_changed_search_dependency_rejects_resume_without_new_embedding(self):
            with self.assertRaises(PlatformError):
                self.platform.stage(self.actor, self.kb, ['hybrid'])
            build = self.latest()
            before = len(self.calls)
            with self.platform._tx(self.actor, self.kb) as conn:
                conn.execute("UPDATE builds SET inputs=jsonb_set(inputs, '{search_dependencies,files,postgres_search.py}', "
                             "to_jsonb(%s::text)) WHERE build_id=%s", ('0' * 64, build['build_id']))
            self.provider.fail = False
            with self.assertRaises(PlatformError) as rejected:
                self.platform.resume_build(self.actor, self.kb, build['build_id'])
            self.assertEqual(rejected.exception.code, 'STALE_BUILD')
            self.assertEqual(len(self.calls), before)
            self.assertEqual(self.latest()['status'], 'STAGING')

        def test_crash_after_search_commit_resumes_without_rebuilding(self):
            original = self.platform.backend.stage

            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('simulated application process interruption')

            with patch.object(self.platform.backend, 'stage', side_effect=interrupted), self.assertRaises(RuntimeError):
                self.platform.stage(self.actor, self.kb, ['bm25'])
            build = self.latest()
            self.assertEqual('STAGING', build['status'])
            with patch.object(self.platform.backend, 'stage', side_effect=AssertionError('must reuse committed generation')):
                result = self.platform.resume_build(self.actor, self.kb, build['build_id'])
            self.assertEqual('VALIDATED', result['status'])
            self.assertEqual([], self.calls)

        def test_stopword_only_queries_use_exact_field(self):
            self.platform.ingest(self.actor, self.kb, 'terms.md', b'IN OR NOT')
            build = self.platform.stage(self.actor, self.kb, ['bm25'])
            self.platform.publish(self.actor, self.kb, build['build_id'])
            for query in ('IN', 'OR', 'NOT'):
                with self.subTest(query=query):
                    self.assertTrue(any('IN OR NOT' in h['text'] for h in
                                        self.platform.search(self.actor, self.kb, query)['hits']))
            self.assertEqual([], self.platform.search(self.actor, self.kb, '!!!')['hits'])

        def test_remote_work_does_not_hold_authority_lock_and_revocation_blocks_publish(self):
            def revoke(texts):
                self.platform.set_source_readers(self.actor, self.source['source_id'], ['new-reader'])
                return [[1., 0., 0.] for _ in texts]

            with patch.object(self.provider, 'documents', side_effect=revoke), self.assertRaises(PlatformError):
                self.platform.stage(self.actor, self.kb, ['hybrid'])
            build = self.latest()
            self.assertEqual('STAGING', build['status'])
            with self.assertRaises(PlatformError):
                self.platform.publish(self.actor, self.kb, build['build_id'])

    return unittest.defaultTestLoader.loadTestsFromTestCase(Builds)


def main():
    parser = argparse.ArgumentParser(description='Explicit live build/embedding recovery checks; synthetic provider, zero remote calls.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    suite = unittest.TestSuite([live_contract_suite(os.environ['KB_DSN']), build_suite(os.environ['KB_DSN'], args.output)])
    with (args.output / 'checks.log').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = dict(status='PASS' if result.wasSuccessful() else 'FAIL', tests=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors), new_remote_calls=0,
                  scope='real PostgreSQL build and embedding jobs; synthetic provider, not answer quality')
    write_json(args.output / 'report.json', report)
    print(report)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()

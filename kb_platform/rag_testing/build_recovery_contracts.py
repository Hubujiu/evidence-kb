from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, update

from kb_platform.application_composition import create_platform
from kb_platform.core import Actor, PlatformError
from kb_platform.knowledge_schema import BUILDS, EMBEDDING_JOBS
from kb_platform.remote import RemoteModelError
from kb_platform.settings import Settings
from kb_platform.storage_catalog import initialize_configuration
from kb_platform.storage_sdk import SpaceKey
from kb_platform.storage_sdk.relational import Clock


class BuildRecoveryContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
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
        configuration = self.make_configuration()
        self.storage_configuration = configuration
        self.platform = create_platform(Settings(self.root / 'originals', self.root / 'identities.json'),
            storage_configuration=configuration, embedding_provider=self.provider)
        self.addCleanup(self.platform.close)
        self.actor = Actor('pg-build-contract-' + uuid4().hex, 'owner')
        self.kb = self.platform.create_kb(self.actor, 'Recovery fixture')['kb_id']
        self.source = self.platform.ingest(self.actor, self.kb, 'one.md', b'# Policy\n\nALPHA backup.\n\nBETA restore.',
            policy={'embedding': True, 'allowed_providers': ['fixture']})

    def make_configuration(self):
        template = json.loads((Path(__file__).resolve().parents[2] / 'config/storage.local.example.json').read_text('utf-8'))
        template['application_id'] += '-' + uuid4().hex
        for row in template['instances'].values():
            row['config']['root'] = str(self.root / Path(row['config']['root']).name)
        return initialize_configuration(template)

    def transaction(self):
        return self.platform.repository.database.transaction(scope={
            'tenant_id': self.actor.tenant_id, 'kb_id': self.kb})

    def latest(self):
        with self.transaction() as conn:
            return conn.execute(select(BUILDS)).fetchone()

    def test_partial_embedding_resume_and_publish(self):
        with self.assertRaises(PlatformError) as raised:
            self.platform.stage(self.actor, self.kb, ['hybrid'])
        self.assertEqual('EMBEDDING_RETRY', raised.exception.code)
        build = self.latest()
        self.assertEqual('STAGING', build['status'])
        with self.transaction() as conn:
            conn.execute(update(EMBEDDING_JOBS).where(EMBEDDING_JOBS.c.status == 'RETRY').values(retry_at=Clock()))
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
        with self.transaction() as conn:
            conn.execute(update(EMBEDDING_JOBS).where(EMBEDDING_JOBS.c.status == 'RETRY').values(retry_at=Clock()))
            inputs = deepcopy(build['inputs'])
            inputs['search_dependencies']['implementation']['files']['kb_platform/search.py'] = '0' * 64
            conn.execute(update(BUILDS).where(BUILDS.c.build_id == build['build_id']).values(inputs=inputs))
        before = list(self.calls)
        self.provider.fail = False
        with self.assertRaises(PlatformError) as raised:
            self.platform.resume_build(self.actor, self.kb, build['build_id'])
        self.assertEqual(raised.exception.code, 'STALE_BUILD')
        self.assertEqual(self.calls, before)
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

    def test_publication_versions_acl_and_missing_original_survive_restart(self):
        reader = Actor(self.actor.tenant_id, 'reader')
        self.platform.set_member(self.actor, self.kb, reader.principal_id, ['kb.read'])
        self.platform.set_source_readers(self.actor, self.source['source_id'], ['reader'])
        build = self.platform.stage(self.actor, self.kb, ['bm25'])
        with self.assertRaises(PlatformError) as missing:
            self.platform.search(reader, self.kb, 'ALPHA')
        self.assertEqual(missing.exception.code, 'NO_PUBLISHED_MANIFEST')
        self.platform.publish(self.actor, self.kb, build['build_id'])
        self.assertTrue(self.platform.publish(self.actor, self.kb, build['build_id'])['idempotent'])
        first = self.platform.search(reader, self.kb, 'ALPHA')
        self.assertTrue(first['hits'])
        restarted = create_platform(Settings(self.root / 'originals', self.root / 'identities.json'),
            storage_configuration=self.storage_configuration, embedding_provider=self.provider)
        self.addCleanup(restarted.close)
        self.assertEqual([hit['citation_id'] for hit in first['hits']],
            [hit['citation_id'] for hit in restarted.search(reader, self.kb, 'ALPHA')['hits']])
        self.platform.set_source_readers(self.actor, self.source['source_id'], [])
        self.assertEqual(restarted.search(reader, self.kb, 'ALPHA')['hits'], [])
        self.platform.set_source_readers(self.actor, self.source['source_id'], ['reader'])
        self.platform.ingest(self.actor, self.kb, 'one.md', b'OMEGAMARKER replacement procedure.',
            readers=['reader'], source_id=self.source['source_id'])
        self.assertEqual(restarted.search(reader, self.kb, 'ALPHA')['hits'], [])
        stale = self.platform.stage(self.actor, self.kb, ['bm25'])
        self.platform.set_source_readers(self.actor, self.source['source_id'], [])
        with self.assertRaises(PlatformError) as changed:
            self.platform.publish(self.actor, self.kb, stale['build_id'])
        self.assertEqual(changed.exception.code, 'STALE_BUILD')
        current = self.platform.stage(self.actor, self.kb, ['bm25'])
        self.platform.publish(self.actor, self.kb, current['build_id'])
        result = restarted.search(self.actor, self.kb, 'OMEGAMARKER')
        self.assertTrue(result['hits'])
        self.assertEqual(restarted.search(reader, self.kb, 'OMEGAMARKER')['hits'], [])
        citation = result['hits'][0]['citation_id']
        resolved = restarted.resolve_citation(self.actor, self.kb, citation)
        reference = restarted.store._bound.reference(self.actor.tenant_id, resolved['original_path'],
                                                     sha256=resolved['original_sha256'])
        root = Path(self.storage_configuration['instances']['originals']['config']['root'])
        original = root.joinpath(*reference.object_key.split('/')).resolve()
        self.assertTrue(original.is_relative_to(self.root))
        original.unlink()
        self.assertEqual(restarted.resolve_citation(self.actor, self.kb, citation)['text'],
                         'OMEGAMARKER replacement procedure.')
        with self.assertRaises(PlatformError) as unavailable:
            restarted.read_original(self.actor, self.kb, citation)
        self.assertEqual(unavailable.exception.code, 'SOURCE_INTEGRITY')

def live_suite(fixtures):
    cases = []
    for engine in ('postgresql', 'mysql'):
        class LiveBuildRecovery(BuildRecoveryContracts):
            def make_configuration(self, engine=engine):
                configuration = super().make_configuration()
                records = configuration['instances']['records']
                configuration['instances']['source'] = deepcopy(records)
                configuration['services']['records.source']['instance'] = 'source'
                records.update(provider=engine, config={'url': fixtures.selected[engine]})
                binding = configuration['bindings']['repository']['primary']
                binding.pop('dataset_id')
                configuration['application_id'] = 'build-recovery-' + uuid4().hex
                for slots in configuration['bindings'].values():
                    for value in slots.values():
                        value.pop('dataset_id', None)
                key = SpaceKey(configuration['application_id'], binding['owner'], binding['space'])
                fixtures.spaces.append((engine, 'kb_' + key.digest[:24]))
                return initialize_configuration(configuration)
        cases.append(unittest.defaultTestLoader.loadTestsFromTestCase(LiveBuildRecovery))
    return unittest.TestSuite(cases)

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import unittest

from kb_platform.database_contracts import SpaceKey
from kb_platform.database_providers import PostgresProvider, SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.knowledge_storage import knowledge_schema
from kb_platform.postgres_search import PostgresSearch
from kb_platform.search import SearchUnavailable, content_order
from kb_platform.search_channels import SearchLexicalAdapter, SearchVectorAdapter
from kb_platform.search_evidence import DocumentEvidenceSource
from kb_platform.search_router import SplitSearch
from kb_platform.storage_adapters.values import SqlDocumentStore
from kb_platform.storage_sdk import ExistingDataset
from .product_fixture import Authority


PUBLIC = 'ORCHIDMARKER Public publication requires owner review.'
PRIVATE = 'CEDARMARKER Restricted publication requires administrator review.'


class _Database:
    def __init__(self, fixtures, engine, name, owner):
        self.fixtures, self.engine, self.name, self.owner = fixtures, engine, name, owner
        self.identity = self.service = self.bound = None

    def open(self):
        fixtures = self.fixtures
        if self.identity is None and self.engine != 'dense_postgresql':
            self.service, self.bound = fixtures.bind(self.engine, self.name, owner=self.owner)
        else:
            self.service = DatabaseService()
            fixtures.services.append(self.service)
            provider = (SQLiteProvider(fixtures.resource_root / 'sqlite') if self.engine == 'sqlite'
                        else PostgresProvider(fixtures.selected[self.engine]))
            self.service.register('search-acceptance', provider)
            space = (self.identity.space if self.identity is not None
                     else SpaceKey('storage-acceptance-' + fixtures.run_id, self.owner, self.name))
            if self.engine != 'sqlite':
                fixtures.spaces.append((self.engine, 'kb_' + space.digest[:24]))
            grant = self.service.grant('search-acceptance', space, 'search-integration',
                expected_dataset_id=None if self.identity is None else self.identity.dataset_id)
            self.bound = self.service.bind(grant)
        if self.identity is not None and self.identity != self.bound.identity:
            raise RuntimeError('Acceptance restart changed the persistent dataset identity')
        self.identity = self.bound.identity
        return self.bound

    def close(self):
        if self.service is not None:
            self.service.close()

    def describe(self):
        if self.engine == 'sqlite':
            return {'engine': self.engine, 'dataset': asdict(self.identity)}
        with self.bound.transaction(write=False) as tx:
            actual = tx.execute('SELECT current_database() AS database,current_schema() AS schema,'
                                'inet_server_port() AS server_port').fetchone()
        if actual['schema'] != self.bound.physical_name:
            raise RuntimeError('Acceptance SQL did not execute in the expected physical namespace')
        return {'engine': self.engine, 'dataset': asdict(self.identity), 'actual': dict(actual),
                'endpoint_port': self.fixtures.selected[self.engine].port}


class _Configuration:
    def __init__(self, fixtures, name, lexical, vector, *, shared=False, source='sqlite'):
        self.fixtures, self.name = fixtures, name
        self.engines = {'bm25': lexical, 'dense': vector, 'source': source}
        self.databases = {}
        for channel, engine in (('bm25', lexical), ('dense', vector)):
            if engine == 'embedded':
                continue
            if channel == 'dense' and shared:
                self.databases[channel] = self.databases['bm25']
            else:
                self.databases[channel] = _Database(fixtures, engine, name + '-' + channel, 'search-channels')
        self.databases['source'] = _Database(fixtures, source, name + '-source', 'search-evidence')
        self.embedded = self.embedded_identity = None

    def open(self):
        from kb_platform.storage_catalog import EmbeddedStorageProvider

        backends = {}
        for resource in dict.fromkeys(self.databases.values()):
            database = resource.open()
            if resource is not self.databases['source']:
                database.prepare(knowledge_schema(database.identity.space.owner_id))
                database.seal()
                backends[resource] = PostgresSearch(database=database)
        if 'embedded' in self.engines.values():
            self.embedded = EmbeddedStorageProvider({'root': str(self.fixtures.resource_root / self.name / 'embedded')})
            self.fixtures.services.append(self.embedded)
            if self.embedded_identity is None:
                space = SpaceKey('storage-acceptance-' + self.fixtures.run_id, 'search-channels', self.name)
                self.embedded_identity = self.embedded.initialize('vector', space)
        channels = {}
        for name, kind in (('bm25', 'lexical'), ('dense', 'vector')):
            if self.engines[name] == 'embedded':
                export = next(export for export in self.embedded.exports() if export.name == kind)
                channels[name] = export.open_space(self.embedded_identity.space,
                                                   ExistingDataset(self.embedded_identity.dataset_id))
            else:
                resource = self.databases[name]
                adapter = SearchLexicalAdapter if name == 'bm25' else SearchVectorAdapter
                channels[name] = adapter(backends[resource], identity=resource.identity)
        source = DocumentEvidenceSource(SqlDocumentStore(self.databases['source'].bound))
        self.router = SplitSearch(channels['bm25'], channels['dense'], source)
        return self.router

    def describe(self, index, *, published):
        router = self.router
        return {'name': self.name, 'index': index, 'published': published,
                'channels': {name: {'engine': self.engines[name], 'dataset': asdict(service.identity),
                                    'backend': type(service.backend).__name__,
                                    'generation': service.verify(router.channel_index(index, name))['generation'],
                                    'physical': (self.databases[name].describe() if name in self.databases
                                                 else {'engine': 'embedded', 'dataset': asdict(service.identity)})}
                             for name, service in (('bm25', router.bm25), ('dense', router.vector))},
                'source': {'engine': self.engines['source'], 'backend': 'SqlDocumentStore',
                           'dataset': asdict(router.source.identity), 'physical': self.databases['source'].describe()},
                'source_sha256': router.verify(index)['units_sha256'],
                'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0}

    def close(self):
        if self.embedded is not None:
            self.embedded.close()
        for resource in reversed(tuple(dict.fromkeys(self.databases.values()))):
            resource.close()


@contextmanager
def _deny_stage(configuration, channel):
    resource = configuration.databases[channel]
    database = resource.bound
    with database._space.migration_engine.begin() as connection:
        quote = connection.dialect.identifier_preparer.quote
        role, schema = quote(database.runtime_role), quote(database.schema)
        target = (schema + '.' + quote('kb_storage_documents') if channel == 'source'
                  else schema + '.' + quote('create_search_generation') + '(text,integer,integer,text)')
        permission = 'INSERT ON TABLE ' if channel == 'source' else 'EXECUTE ON FUNCTION '
        connection.exec_driver_sql('REVOKE ' + permission + target + ' FROM ' + role)
    try:
        yield
    finally:
        with database._space.migration_engine.begin() as connection:
            connection.exec_driver_sql('GRANT ' + permission + target + ' TO ' + role)


def _authority():
    authority = Authority([('public.txt', PUBLIC), ('private.txt', PRIVATE)])
    for unit in authority.units:
        unit['vector'] = [1.0, 0.0, 0.0] if unit['source_id'] == 's0' else [0.0, 1.0, 0.0]
    public = next(unit for unit in authority.units if unit['source_id'] == 's0')
    authority.units.append({**deepcopy(public), 'kind': 'Page', 'unit_id': 'public-page', 'citation_id': 'public-page'})
    return authority


def suite(fixtures):
    class StorageSearchIntegration(unittest.TestCase):
        def configuration(self, name, lexical, vector, *, shared=False, source='sqlite'):
            selected = _Configuration(fixtures, name, lexical, vector, shared=shared, source=source)
            self.addCleanup(selected.close)
            selected.open()
            return selected

        def evidence(self, router, index, authority):
            expected = sorted(deepcopy(authority.units), key=content_order)
            self.assertEqual(router.read_units(index), expected)
            self.assertEqual(router.verify(index)['count'], len(expected))
            originals = {'s0': PUBLIC, 's1': PRIVATE}
            for unit in router.read_units(index):
                self.assertEqual(unit['source_version_id'], sha256(originals[unit['source_id']].encode()).hexdigest())
                self.assertEqual(unit['text'], originals[unit['source_id']])
            lexical = router.search(index, 'tenant', 'scope', authority.rows, 'ORCHIDMARKER', 3)
            self.assertTrue(lexical)
            self.assertEqual({unit['source_id'] for unit in lexical}, {'s0'})
            self.assertEqual({unit['text'] for unit in lexical}, {PUBLIC})
            dense = router.dense(index, 'tenant', 'scope', authority.rows, [1, 0, 0], 1)
            self.assertEqual([unit['source_id'] for unit in dense], ['s0'])
            restricted = router.dense(index, 'tenant', 'scope', authority.rows[:1], [0, 1, 0], 1)
            self.assertTrue(restricted)
            self.assertEqual({unit['source_id'] for unit in restricted}, {'s0'})
            self.assertEqual(router.search(index, 'tenant', 'scope', authority.rows[:1], 'CEDARMARKER', 3), [])
            self.assertEqual(router.dense(index, 'other-tenant', 'scope', authority.rows, [1, 0, 0], 3), [])
            revoked = [{**authority.rows[0], 'parse_snapshot_id': 'revoked-parse'}]
            self.assertEqual(router.dense(index, 'tenant', 'scope', revoked, [1, 0, 0], 3), [])
            self.assertEqual(router.search(index, 'tenant', 'scope', revoked, 'ORCHIDMARKER', 3), [])
            pages = router.dense_pages(index, 'tenant', 'scope', 'build', authority.rows, [1, 0, 0], 3, ())
            self.assertEqual([page['citation_id'] for page in pages], ['public-page'])
            self.assertEqual(router.dense_pages(index, 'tenant', 'scope', 'build', authority.rows,
                                                [1, 0, 0], 3, ('public-page',)), [])
            self.assertTrue(router.expand(index, 'tenant', 'scope', lexical[0]))
            for channel, service in (('bm25', router.bm25), ('dense', router.vector)):
                records = service.read_records(router.channel_index(index, channel))
                self.assertEqual({record['id'] for record in records}, {unit['unit_id'] for unit in expected})
                self.assertTrue(all(len(record['metadata']['source_sha256']) == 64 for record in records))
                if channel == 'dense':
                    self.assertTrue(all('text' not in record and 'anchors' not in record['metadata'] for record in records))
                else:
                    self.assertTrue(all('vector' not in record for record in records))

        def publish(self, router, index, label):
            manifest = {'index_name': index, 'tenant_id': 'tenant', 'kb_id': 'scope', 'synthetic_label': label}
            router.seal(index, label)
            self.assertTrue(router.is_sealed(index, label))
            router.publish_manifest(manifest, label)
            self.assertEqual(router.current_manifest('tenant', 'scope'), (manifest, label))
            return manifest, label

        def exercise(self, name, lexical, vector, *, shared=False, source='sqlite'):
            selected = self.configuration(name, lexical, vector, shared=shared, source=source)
            router, authority = selected.router, _authority()
            index = router.index_name('tenant', 'scope', 'a' * 32)
            router.stage(index, authority.units, vector_dimension=3)
            self.evidence(router, index, authority)
            published = self.publish(router, index, name)
            first = selected.describe(index, published=True)
            if shared:
                self.assertEqual(first['channels']['bm25']['dataset'], first['channels']['dense']['dataset'])
                self.assertNotEqual(first['channels']['bm25']['generation'], first['channels']['dense']['generation'])
            if name == 'pg-distinct-databases':
                a, b = first['channels']['bm25']['physical'], first['channels']['dense']['physical']
                self.assertNotEqual((a['endpoint_port'], a['actual']['database']),
                                    (b['endpoint_port'], b['actual']['database']))
                self.assertNotEqual(first['channels']['bm25']['dataset'], first['channels']['dense']['dataset'])
            selected.close()
            restarted = selected.open()
            self.assertEqual(restarted.current_manifest('tenant', 'scope'), published)
            self.assertTrue(restarted.is_sealed(index, name))
            self.evidence(restarted, index, authority)
            self.assertEqual(selected.describe(index, published=True), first)
            next_index = restarted.index_name('tenant', 'scope', 'b' * 32)
            next_authority = _authority()
            for unit in next_authority.units:
                unit['build_id'] = 'next-build'
            restarted.stage(next_index, next_authority.units, vector_dimension=3)
            next_manifest = self.publish(restarted, next_index, name + '-after-restart')
            self.assertNotEqual(next_manifest, published)
            self.assertTrue(restarted.is_sealed(index, name))
            self.assertEqual(restarted.read_units(index), sorted(authority.units, key=content_order))
            self.assertEqual(restarted.read_units(next_index), sorted(next_authority.units, key=content_order))
            fixtures.observations.setdefault('search_contract_matrix', []).append(first | {
                'restart_preserved': True, 'new_build': selected.describe(next_index, published=True),
                'bm25': 'actual pg_textsearch or Tantivy', 'dense': 'actual pgvector or numpy',
                'acl_revocation_and_ranking_prefilter': True})

        def test_pg_channels_share_one_actual_space(self):
            self.exercise('pg-same-space', 'postgresql', 'postgresql', shared=True, source='postgresql')

        def test_pg_channels_use_two_actual_databases(self):
            self.exercise('pg-distinct-databases', 'postgresql', 'dense_postgresql')

        def test_pg_lexical_and_embedded_vector(self):
            self.exercise('pg-lexical-embedded-vector', 'postgresql', 'embedded', source='postgresql')

        def test_embedded_lexical_and_pg_vector(self):
            self.exercise('embedded-lexical-pg-vector', 'embedded', 'dense_postgresql')

        def test_embedded_channels_share_one_actual_space(self):
            self.exercise('embedded-both', 'embedded', 'embedded')

        def failure(self, channel):
            selected = self.configuration('fault-' + channel, 'postgresql', 'dense_postgresql', source='postgresql')
            router, authority = selected.router, _authority()
            baseline = router.index_name('tenant', 'scope', 'c' * 32)
            router.stage(baseline, authority.units, 3)
            published = self.publish(router, baseline, 'baseline')
            pending = router.index_name('tenant', 'scope', 'd' * 32)
            with _deny_stage(selected, channel):
                with self.assertRaises(SearchUnavailable):
                    router.stage(pending, authority.units, 3)
                with self.assertRaises(SearchUnavailable):
                    router.publish_manifest({'index_name': pending}, 'pending')
                self.assertEqual(router.current_manifest('tenant', 'scope'), published)
                self.assertFalse(router.is_sealed(pending, 'pending'))
            preserved = {}
            if channel != 'source':
                preserved['source'] = router.source.verify(pending)
            if channel == 'dense':
                preserved['bm25'] = router.bm25.verify(router.channel_index(pending, 'bm25'))
            router.stage(pending, authority.units, 3)
            for name, metadata in preserved.items():
                service = router.source if name == 'source' else router.bm25
                target = pending if name == 'source' else router.channel_index(pending, name)
                self.assertEqual(service.verify(target), metadata)
            self.evidence(router, pending, authority)
            self.publish(router, pending, 'pending')
            fixtures.observations.setdefault('search_contract_faults', []).append(selected.describe(pending, published=True) | {
                'failed_channel': channel, 'injection': 'actual PostgreSQL runtime privilege revoked and restored',
                'previous_publication_preserved': True, 'partial_publication_rejected': True,
                'ready_channels_preserved': sorted(preserved), 'retry_completed': True})

        def test_actual_source_write_failure_does_not_publish(self):
            self.failure('source')

        def test_actual_lexical_stage_failure_does_not_publish(self):
            self.failure('bm25')

        def test_actual_vector_stage_failure_does_not_publish(self):
            self.failure('dense')

        def test_actual_pg_authoritative_corruption_blocks_sealing_publication_and_delivery(self):
            selected = self.configuration('corrupt-authority', 'postgresql', 'dense_postgresql', source='postgresql')
            router, authority = selected.router, _authority()
            baseline = router.index_name('tenant', 'scope', 'e' * 32)
            router.stage(baseline, authority.units, 3)
            published = self.publish(router, baseline, 'uncorrupted-baseline')
            pending = router.index_name('tenant', 'scope', 'f' * 32)
            router.stage(pending, authority.units, 3)
            source = router.source
            manifest = source.documents.get(source._key(pending, 'manifest'))
            key = source._key(pending, manifest.value['records'][0][1])
            original = source.documents.get(key)
            changed = source.documents.put(key, original.value | {'text': 'CORRUPTED authoritative original'},
                                           expected_revision=original.revision)
            for operation in (lambda: router.verify(pending), lambda: router.seal(pending, 'repaired'),
                              lambda: router.publish_manifest({'index_name': pending}, 'repaired')):
                with self.assertRaises(SearchUnavailable):
                    operation()
            self.assertEqual(router.current_manifest('tenant', 'scope'), published)
            repaired = source.documents.put(key, original.value, expected_revision=changed.revision)
            self.publish(router, pending, 'repaired')
            changed = source.documents.put(key, original.value | {'text': 'CORRUPTED after publication'},
                                           expected_revision=repaired.revision)
            with self.assertRaises(SearchUnavailable):
                router.dense(pending, 'tenant', 'scope', authority.rows, [1, 0, 0], 1)
            with self.assertRaises(SearchUnavailable):
                router.current_manifest('tenant', 'scope')
            source.documents.put(key, original.value, expected_revision=changed.revision)
            self.assertTrue(router.is_sealed(pending, 'repaired'))
            fixtures.observations['search_contract_corruption'] = selected.describe(pending, published=True) | {
                'actual_pg_document_mutation': True, 'unsealed_corruption_blocks_publication': True,
                'published_corruption_blocks_delivery': True, 'source_repair_verified': True}

    return unittest.defaultTestLoader.loadTestsFromTestCase(StorageSearchIntegration)

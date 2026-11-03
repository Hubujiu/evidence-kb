from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from weakref import WeakKeyDictionary

from sqlalchemy import create_engine, delete, update
from sqlalchemy.engine import make_url
from sqlalchemy.sql import Select

from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.database_contracts import DatabaseError, Migration, SpaceKey
from kb_platform.database_providers import PostgresProvider, SQLiteProvider
from kb_platform.documents import parse_native_document
from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.embedding_jobs import EmbeddingJobError, EmbeddingJobs, digest
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import EMBEDDING_JOBS, METADATA, PARSE_SNAPSHOTS, SOURCE_VERSIONS
from kb_platform.knowledge_sql import OPERATIONS
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.storage import OriginalStore
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_sdk import ExistingDataset


_RAW_JOB_DATABASES = WeakKeyDictionary()


class _PostgresRepositoryResources:
    def __init__(self, provider, migration_url, schema):
        self.provider, self.migration_url, self.schema = provider, migration_url, schema

    def close(self):
        if self.provider is None:
            return
        self.provider.close()
        self.provider = None
        engine = create_engine(self.migration_url, isolation_level='AUTOCOMMIT')
        try:
            with engine.connect() as connection:
                quote = connection.dialect.identifier_preparer.quote
                connection.exec_driver_sql('DROP SCHEMA IF EXISTS ' + quote(self.schema) + ' CASCADE')
        finally:
            engine.dispose()


def postgres_repository(dsn):
    runtime = make_url(dsn)
    migration_dsn = os.environ.get('KB_INTEGRATION_MIGRATION_DSN')
    if not migration_dsn:
        raise ValueError('CONTROLLED_KNOWLEDGE_MIGRATION_DATABASE_REQUIRED')
    migration = make_url(migration_dsn)
    if (runtime.host not in {'127.0.0.1', 'localhost'} or runtime.port != 55432
            or not runtime.database or not runtime.database.startswith('rust_plugin_acceptance_')
            or any(getattr(runtime, key) != getattr(migration, key) for key in ('host', 'port', 'database'))
            or runtime.get_backend_name() != 'postgresql' or migration.get_backend_name() != 'postgresql'):
        raise ValueError('CONTROLLED_KNOWLEDGE_MIGRATION_DATABASE_REQUIRED')
    provider = SqlStorageProvider(PostgresProvider(runtime, migration_url=migration))
    space = SpaceKey('live-contracts', 'knowledge', 'jobs-' + uuid4().hex)
    owner = _PostgresRepositoryResources(provider, migration.set(drivername='postgresql+psycopg'),
                                         'kb_' + space.digest[:24])
    try:
        identity = provider.initialize('structured', space)
        exports = {export.name: export for export in provider.exports()}
        database = exports['structured'].open_space(space, ExistingDataset(identity.dataset_id))
        repository = KnowledgeRepository(database=database)
        _RAW_JOB_DATABASES[repository] = exports['relational'].open_space(space, ExistingDataset(identity.dataset_id))
        return repository, owner
    except BaseException:
        owner.close()
        raise


@contextmanager
def raw_job_transaction(jobs, tenant_id, kb_id):
    from kb_platform.database_providers.postgres import pg_transaction

    with pg_transaction(_RAW_JOB_DATABASES[jobs.repository], tenant_id, kb_id) as connection:
        connection.execute("SELECT set_config('storage_scope.tenant_id',%s,true), "
                           "set_config('storage_scope.kb_id',%s,true)", (tenant_id, kb_id))
        yield connection


class LocalEmbeddingProvider:
    provider_id, model = 'offline-contract', 'fixture-v1'
    dimension, max_batch_size = 2, 1
    manifest = {'provider': provider_id, 'model': model, 'dimension': dimension}

    def __init__(self):
        self.calls = []

    def documents(self, texts, **kwargs):
        self.calls.append(tuple(texts))
        return [[1., 0.] for _ in texts]

    def query(self, text):
        return [1., 0.]


def exercise_knowledge_repository(check, database, root):
    root = Path(root)
    repository = KnowledgeRepository(database=database)
    provider = LocalEmbeddingProvider()
    search = EmbeddedSearch(root / 'search')
    search.supports_resume_build = True
    store = OriginalStore(root / 'originals')
    jobs = EmbeddingJobs(repository)
    platform = Platform(repository=repository, search=search, store=store,
        parser=parse_native_document, embedding_jobs=jobs, embedding_provider=provider)
    owner, reader = Actor('knowledge-contract', 'owner'), Actor('knowledge-contract', 'reader')
    try:
        kb_id = platform.create_kb(owner, 'Portable knowledge')['kb_id']
        platform.set_member(owner, kb_id, reader.principal_id, ['kb.read'])
        source = b'Immutable synthetic storage contract policy.'
        ingested = platform.ingest(owner, kb_id, 'contract.md', source, readers=['reader'],
            policy={'embedding': True, 'allowed_providers': [provider.provider_id]})
        build = platform.stage(owner, kb_id, ['bm25', 'dense'])
        check.assertEqual(build['status'], 'VALIDATED')
        check.assertEqual(platform.publish(owner, kb_id, build['build_id'])['status'], 'PUBLISHED')
        check.assertTrue(platform.publish(owner, kb_id, build['build_id'])['idempotent'])
        result = platform.search(reader, kb_id, 'synthetic')
        check.assertEqual(result['context'], source.decode())
        citation_id = result['hits'][0]['citation_id']
        check.assertEqual(platform.read_original(reader, kb_id, citation_id), source)
        check.assertEqual(platform.resolve_citation(reader, kb_id, citation_id)['original_sha256'],
                          hashlib.sha256(source).hexdigest())
        check.assertEqual(platform.search(reader, kb_id, 'synthetic', 'dense')['context'], source.decode())
        check.assertEqual(len(provider.calls), 1)
        with check.assertRaises(PlatformError):
            platform.search(Actor('another-tenant', reader.principal_id), kb_id, 'synthetic')
        with check.assertRaises(PlatformError):
            platform.search(Actor(owner.tenant_id, 'outsider'), kb_id, 'synthetic')
        check.assertEqual(platform.search(reader, kb_id, 'synthetic')['manifest_id'], build['build_id'])
        platform.set_source_readers(owner, ingested['source_id'], [])
        check.assertEqual(platform.search(reader, kb_id, 'synthetic')['hits'], [])
        with check.assertRaises(PlatformError):
            platform.resolve_citation(reader, kb_id, citation_id)
        stale = platform.stage(owner, kb_id)
        platform.set_source_policy(owner, ingested['source_id'], {'preview': False})
        with check.assertRaisesRegex(PlatformError, 'Build inputs or authorization changed'):
            platform.publish(owner, kb_id, stale['build_id'])
        with repository.transaction(owner, kb_id) as transaction:
            current = transaction.execute(K.KB_MEMBERSHIP,
                (owner.tenant_id, kb_id, owner.principal_id)).fetchone()
        check.assertEqual(current['manifest_id'], build['build_id'])
        scope = (owner.tenant_id, kb_id, 'expired-job')
        units, audit = [{'text': 'unknown physical request'}], {'principal': owner.principal_id}
        jobs._prepare(*scope, provider, units, audit)
        dependency = None
        with jobs._tx(*scope[:2]) as transaction:
            dependency = transaction.execute(K.JOBS_INPUTS, scope).fetchone()['dependency_hash']
        claimed = jobs._claim(*scope, 0, dependency)
        with jobs._tx(*scope[:2]) as transaction:
            result = transaction._transaction.execute(update(EMBEDDING_JOBS).where(
                EMBEDDING_JOBS.c.build_id == scope[2], EMBEDDING_JOBS.c.batch_no == 0)
                .values(lease_until=claimed['lease_until'] - timedelta(days=1)))
            check.assertEqual(result.rowcount, 1)
        calls = len(provider.calls)
        jobs.reconcile(*scope, provider, lambda: True)
        with check.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_UNKNOWN'):
            jobs.run(*scope, provider, units, lambda: True, audit)
        check.assertEqual(len(provider.calls), calls)
        check.assertEqual(jobs.status(*scope)[0]['status'], 'UNKNOWN')
        check.assertTrue(repository.probe()['ready'])
        return {'knowledge_build': build['status'],
            'cross_tenant': 'DENIED', 'revoked_citation': 'DENIED',
            'expired_job': 'UNKNOWN', 'external_model_calls': 0}
    finally:
        search.close()
        store.close()


def exercise_job_lock_deadlines(check, repository):
    jobs, provider = EmbeddingJobs(repository, lease_seconds=1), LocalEmbeddingProvider()
    tenant, kb_id = 'lease-contract-' + uuid4().hex, 'scope'
    scope, units, audit = (tenant, kb_id, 'finish'), [{'text': 'lease'}], {}
    _, dependency = jobs._prepare(*scope, provider, units, audit)
    claimed = jobs._claim(*scope, 0, dependency)
    with jobs._tx(tenant, kb_id) as transaction:
        now = transaction.execute(K.JOB_CLOCK).fetchone()['db_now']
    with jobs._tx(*scope[:2]) as transaction:
        result = transaction._transaction.execute(update(EMBEDDING_JOBS).where(
            EMBEDDING_JOBS.c.build_id == scope[2], EMBEDDING_JOBS.c.batch_no == 0)
            .values(lease_until=now + timedelta(seconds=.2)))
        check.assertEqual(result.rowcount, 1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with jobs._tx(tenant, kb_id) as blocker:
            blocker.execute(K.JOB_LOCK, (*scope, 0))
            entered, transaction = Event(), jobs._tx

            def observe_finish(*args, **kwargs):
                entered.set()
                return transaction(*args, **kwargs)

            with patch.object(jobs, '_tx', side_effect=observe_finish):
                finishing = executor.submit(jobs._finish, claimed, vectors=[[1., 0.]])
                check.assertTrue(entered.wait(2))
                time.sleep(.4)
        with check.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEASE_LOST'):
            finishing.result(timeout=5)
    check.assertEqual(jobs.status(*scope)[0]['status'], 'RUNNING')
    scope = (tenant, kb_id, 'claim')
    _, dependency = jobs._prepare(*scope, provider, units, audit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with jobs._tx(tenant, kb_id) as blocker:
            blocker.execute(K.CIRCUIT_LOCK, (tenant, kb_id, digest(provider.manifest)))
            entered, transaction = Event(), jobs._tx

            def observe_claim(*args, **kwargs):
                entered.set()
                return transaction(*args, **kwargs)

            with patch.object(jobs, '_tx', side_effect=observe_claim):
                claiming = executor.submit(jobs._claim, *scope, 0, dependency)
                check.assertTrue(entered.wait(2))
                time.sleep(1.1)
        current = claiming.result(timeout=5)
    with jobs._tx(tenant, kb_id) as transaction:
        now = transaction.execute(K.JOB_CLOCK).fetchone()['db_now']
    check.assertGreater((current['lease_until'] - now).total_seconds(), .5)
    return {'finish_after_lock_expiry': 'REJECTED', 'claim_lease_after_lock': 'FRESH'}


class KnowledgePersistenceContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.provider = SqlStorageProvider(SQLiteProvider(self.root / 'database'))
        self.addCleanup(self.provider.close)
        self.space = SpaceKey('contracts', 'knowledge', 'repository')
        self.identity = self.provider.initialize('structured', self.space)
        self.database = next(export for export in self.provider.exports() if export.name == 'structured')\
            .open_space(self.space, ExistingDataset(self.identity.dataset_id))

    def test_real_sqlite_knowledge_ingest_publish_acl_dense_and_unknown(self):
        exercise_knowledge_repository(self, self.database, self.root)

    def test_business_uses_structured_capabilities_without_provider_brand_metadata(self):
        database = self.database

        class IndependentStore:
            identity = database.identity

            def prepare(self, schema):
                return database.prepare(schema)

            def seal(self):
                database.seal()
                return self

            def transaction(self, **options):
                return database.transaction(**options)

        exercise_knowledge_repository(self, IndependentStore(), self.root)

    def test_operation_table_is_complete_and_ids_have_no_sql(self):
        self.assertEqual(set(K), set(OPERATIONS))
        self.assertTrue(all(query.value == query.value.lower() and ' ' not in query.value for query in K))

    def test_old_migration_hashes_are_archived_without_runtime_schema_selection(self):
        archive = json.loads((Path(__file__).resolve().parents[2] /
            'config/storage/legacy-knowledge-migration-v1.json').read_text('utf-8'))
        self.assertEqual(archive['engines']['postgresql']['migrations'][0]['digest'],
            'c65c97d6918c75a741647e87131df806f71aa22ccb70d4ba3662f75dd9a91235')
        for engine in archive['engines'].values():
            for migration in engine['migrations']:
                self.assertEqual(Migration(migration['version'], tuple(migration['statements'])).digest,
                                 migration['digest'])
        self.assertNotIn('search_generations', METADATA.tables)
        self.assertEqual(set(METADATA.tables) & {'sessions', 'events', 'requests', 'artifacts'},
                         {'sessions', 'events', 'requests', 'artifacts'})
        self.assertEqual(len(METADATA.tables), 16)

    def test_immutable_source_identity_is_idempotent_and_rejects_changed_content(self):
        repository = KnowledgeRepository(database=self.database)
        actor = Actor('immutable', 'owner')
        with repository.transaction(actor) as transaction:
            transaction.execute(K.SOURCE_INSERT, ('immutable', 'source', 'owner', [], {}))
            version = ('immutable', 'source', 'version', 'name.md', 'original', 'a' * 64, 'text/markdown')
            transaction.execute(K.SOURCE_VERSION_INSERT, version)
            transaction.execute(K.SOURCE_VERSION_INSERT, version)
            parsed = ('immutable', 'source', 'version', 'parse', {'text': 'unchanged', 'flags': [True, None, 1]})
            transaction.execute(K.PARSE_INSERT, parsed)
            transaction.execute(K.PARSE_INSERT, parsed)
            with self.assertRaisesRegex(PlatformError, 'different content'):
                transaction.execute(K.SOURCE_VERSION_INSERT, (*version[:-1], 'changed/type'))
            with self.assertRaisesRegex(PlatformError, 'different content'):
                transaction.execute(K.PARSE_INSERT, (*parsed[:-1], {'text': 'changed'}))

    def test_transaction_rejects_unregistered_queries_and_scope_replacement(self):
        repository = KnowledgeRepository(database=self.database)
        actor = Actor('tenant-a', 'reader')
        with repository.transaction(actor, 'kb-a') as transaction:
            with self.assertRaisesRegex(ValueError, 'UNREGISTERED_KNOWLEDGE_QUERY'):
                transaction.execute('SELECT 1')
            for query, params in ((K.AUTHORITY_REVISION, ('tenant-b',)),
                (K.KB_MEMBERSHIP, ('tenant-a', 'kb-b', 'reader')),
                (K.KB_MEMBERSHIP, ('tenant-a', 'kb-a', 'somebody-else'))):
                with self.subTest(query=query, params=params), self.assertRaises(PlatformError):
                    transaction.execute(query, params)

    def test_rollback_and_readonly_are_real_transactions(self):
        repository = KnowledgeRepository(database=self.database)
        actor = Actor('rollback', 'reader')
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with repository.transaction(actor) as transaction:
                transaction.execute(K.AUTHORITY_ENSURE, (actor.tenant_id,))
                raise RuntimeError('rollback')
        with repository.transaction(actor, write=False) as transaction:
            self.assertIsNone(transaction.execute(K.AUTHORITY_REVISION, (actor.tenant_id,)).fetchone())
            with self.assertRaisesRegex(ValueError, 'WRITE_TRANSACTION_REQUIRED'):
                transaction.execute(K.AUTHORITY_ENSURE, (actor.tenant_id,))

    def test_structured_appendonly_guard_rejects_runtime_update_and_delete(self):
        repository = KnowledgeRepository(database=self.database)
        with repository.transaction(Actor('immutable', 'owner')) as transaction:
            with self.assertRaises(DatabaseError):
                transaction._transaction.execute(update(SOURCE_VERSIONS).values(filename='changed'))
            with self.assertRaises(DatabaseError):
                transaction._transaction.execute(delete(PARSE_SNAPSHOTS))
        self.assertTrue(repository.probe()['ready'])

    def test_concurrent_authority_bumps_do_not_lose_updates(self):
        repository = KnowledgeRepository(database=self.database)
        actor = Actor('parallel', 'writer')
        with repository.transaction(actor) as transaction:
            transaction.execute(K.AUTHORITY_ENSURE, (actor.tenant_id,))

        def bump(number):
            with repository.transaction(actor) as transaction:
                transaction.execute(K.AUTHORITY_LOCK, (actor.tenant_id,))
                transaction.execute(K.AUTHORITY_BUMP, (actor.tenant_id,))

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(bump, range(12)))
        with repository.transaction(actor, write=False) as transaction:
            self.assertEqual(transaction.execute(K.AUTHORITY_REVISION, (actor.tenant_id,)).fetchone()['revision'], 12)

    def test_hundred_plus_citations_build_with_explicit_domain_statement_budget(self):
        repository = KnowledgeRepository(database=self.database)
        search, store = EmbeddedSearch(self.root / 'large-search'), OriginalStore(self.root / 'large-originals')
        self.addCleanup(search.close)
        self.addCleanup(store.close)
        platform = Platform(repository=repository, search=search, store=store,
            parser=parse_native_document, embedding_jobs=EmbeddingJobs(repository))
        actor = Actor('many-citations', 'owner')
        kb_id = platform.create_kb(actor, 'Many citations')['kb_id']
        content = '\n\n'.join(f'Synthetic paragraph {number} contains immutable evidence.' for number in range(125))
        platform.ingest(actor, kb_id, 'many.md', content.encode())
        build = platform.stage(actor, kb_id)
        self.assertGreater(build['unit_count'], 100)
        platform.publish(actor, kb_id, build['build_id'])
        with repository.transaction(actor, kb_id, write=False) as transaction:
            citations = transaction.execute(K.CITATIONS_FOR_BUILD, (actor.tenant_id, kb_id, build['build_id'])).fetchall()
        self.assertEqual(len(citations), build['unit_count'])
        self.assertTrue(platform.search(actor, kb_id, 'paragraph')['hits'])

    def test_reopen_provider_preserves_dataset_schema_and_knowledge(self):
        repository = KnowledgeRepository(database=self.database)
        actor = Actor('restart', 'owner')
        platform = Platform(repository=repository, search=object(), store=object(),
            parser=parse_native_document, embedding_jobs=EmbeddingJobs(repository))
        kb_id = platform.create_kb(actor, 'Restart')['kb_id']
        self.provider.close()
        replacement = SqlStorageProvider(SQLiteProvider(self.root / 'database'))
        self.addCleanup(replacement.close)
        reopened = next(export for export in replacement.exports() if export.name == 'structured')\
            .open_space(self.space, ExistingDataset(self.identity.dataset_id))
        platform.repository = KnowledgeRepository(database=reopened)
        self.assertEqual(reopened.identity, self.identity)
        self.assertEqual(platform.list_kbs(actor)[0]['kb_id'], kb_id)

    def test_finish_checks_current_sqlite_clock_after_pause(self):
        repository = KnowledgeRepository(database=self.database)
        jobs, provider = EmbeddingJobs(repository), LocalEmbeddingProvider()
        scope = ('paused', 'scope', 'build')
        _, dependency = jobs._prepare(*scope, provider, [{'text': 'one'}], {})
        row = jobs._claim(*scope, 0, dependency)
        with jobs._tx(*scope[:2]) as transaction:
            now = transaction.execute(K.JOB_CLOCK).fetchone()['db_now']
        with jobs._tx(*scope[:2]) as transaction:
            result = transaction._transaction.execute(update(EMBEDDING_JOBS)
                .values(lease_until=now + timedelta(seconds=.05)))
            self.assertEqual(result.rowcount, 1)
        with jobs._tx(*scope[:2]) as transaction:
            execute = transaction._transaction.execute

            def paused(statement, values=None):
                result = execute(statement, values)
                if isinstance(statement, Select) and EMBEDDING_JOBS in statement.get_final_froms():
                    time.sleep(.1)
                return result

            with patch.object(transaction._transaction, 'execute', side_effect=paused):
                result = transaction.execute(K.JOB_FINISH,
                    ('COMPLETE', [[1., 0.]], 'hash', None, False, None,
                     *scope, 0, row['request_id'], 'RUNNING', 'RUNNING', False))
        self.assertIsNone(result.fetchone())
        self.assertEqual(jobs.status(*scope)[0]['status'], 'RUNNING')

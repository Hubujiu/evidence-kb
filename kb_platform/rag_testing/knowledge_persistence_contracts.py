from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.exc import DBAPIError

from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.database_contracts import SpaceKey, SqlStatement
from kb_platform.database_providers import PostgresProvider, SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.documents import parse_native_document
from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.embedding_jobs import EmbeddingJobError, EmbeddingJobs, digest
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import postgres_schema, sqlite_schema
from kb_platform.knowledge_sql import OPERATIONS
from kb_platform.knowledge_storage import DEFAULT_SPACE, KnowledgeRepository
from kb_platform.storage import OriginalStore
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_sdk import ExistingDataset


def postgres_repository(dsn):
    owner = DatabaseService()
    owner.register('knowledge', PostgresProvider(dsn, provision=False, schema='public'))
    try:
        database = owner.bind(owner.grant('knowledge', DEFAULT_SPACE, 'knowledge-contract'))
        database.seal()
        return KnowledgeRepository(database=database, prepare=False), owner
    except BaseException:
        owner.close()
        raise


def raw_job_transaction(jobs, tenant_id, kb_id):
    from kb_platform.database_providers.postgres import pg_transaction

    return pg_transaction(jobs.repository.database, tenant_id, kb_id)


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
        restarted = KnowledgeRepository(database=database, prepare=False)
        platform.repository = restarted
        platform.embedding_jobs = EmbeddingJobs(restarted)
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
            transaction._transaction.execute(SqlStatement('UPDATE embedding_jobs SET lease_until=:deadline '
                'WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND batch_no=0',
                {'deadline': (claimed['lease_until'] - timedelta(days=1)).isoformat(),
                 'tenant_id': scope[0], 'kb_id': scope[1], 'build_id': scope[2]}, expected_rows=1))
        calls = len(provider.calls)
        jobs.reconcile(*scope, provider, lambda: True)
        with check.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_UNKNOWN'):
            jobs.run(*scope, provider, units, lambda: True, audit)
        check.assertEqual(len(provider.calls), calls)
        check.assertEqual(jobs.status(*scope)[0]['status'], 'UNKNOWN')
        check.assertTrue(repository.probe()['ready'])
        return {'engine': database.info.engine, 'knowledge_build': build['status'],
            'cross_tenant': 'DENIED', 'revoked_citation': 'DENIED',
            'expired_job': 'UNKNOWN', 'external_model_calls': 0}
    finally:
        search.close()
        store.close()


def exercise_job_lock_deadlines(check, repository):
    from kb_platform import knowledge_storage

    jobs, provider = EmbeddingJobs(repository, lease_seconds=1), LocalEmbeddingProvider()
    tenant, kb_id = 'lease-contract-' + uuid4().hex, 'scope'
    scope, units, audit = (tenant, kb_id, 'finish'), [{'text': 'lease'}], {}
    _, dependency = jobs._prepare(*scope, provider, units, audit)
    claimed = jobs._claim(*scope, 0, dependency)
    with jobs._tx(tenant, kb_id) as transaction:
        now = transaction.execute(K.JOB_CLOCK).fetchone()['db_now']
    with jobs._tx(*scope[:2]) as transaction:
        transaction._transaction.execute(SqlStatement('UPDATE embedding_jobs SET lease_until=:deadline '
            'WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND batch_no=0',
            {'deadline': (now + timedelta(seconds=.2)).isoformat(),
             'tenant_id': tenant, 'kb_id': kb_id, 'build_id': scope[2]}, expected_rows=1))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with jobs._tx(tenant, kb_id) as blocker:
            blocker.execute(K.JOB_LOCK, (*scope, 0))
            entered, operation = Event(), knowledge_storage.execute_operation

            def observe_finish(*args, **kwargs):
                if args[2] == K.JOB_FINISH:
                    entered.set()
                return operation(*args, **kwargs)

            with patch.object(knowledge_storage, 'execute_operation', side_effect=observe_finish):
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
            entered, operation = Event(), knowledge_storage.execute_operation

            def observe_claim(*args, **kwargs):
                if args[2] == K.CIRCUIT_LOCK:
                    entered.set()
                return operation(*args, **kwargs)

            with patch.object(knowledge_storage, 'execute_operation', side_effect=observe_claim):
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
        self.identity = self.provider.initialize('relational', self.space)
        self.database = self.provider.exports()[0].open_space(self.space, ExistingDataset(self.identity.dataset_id))

    def test_real_sqlite_knowledge_ingest_publish_acl_dense_and_unknown(self):
        exercise_knowledge_repository(self, self.database, self.root)

    def test_operation_table_is_complete_and_ids_have_no_sql(self):
        self.assertEqual(set(K), set(OPERATIONS))
        self.assertTrue(all(query.value == query.value.lower() and ' ' not in query.value for query in K))

    def test_existing_postgres_migration_one_hash_is_frozen(self):
        self.assertEqual(postgres_schema('knowledge').migrations[0].digest,
            'c65c97d6918c75a741647e87131df806f71aa22ccb70d4ba3662f75dd9a91235')
        statements = '\n'.join(sqlite_schema('knowledge').migrations[0].statements)
        self.assertNotIn('search_generations', statements)
        self.assertNotIn('CREATE EXTENSION', statements)

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

    def test_sqlite_appendonly_guard_rejects_runtime_update_and_delete(self):
        repository = KnowledgeRepository(database=self.database)
        with self.database.transaction() as transaction:
            with self.assertRaises(DBAPIError):
                transaction.execute(SqlStatement("UPDATE source_versions SET filename='changed'"))
            with self.assertRaises(DBAPIError):
                transaction.execute(SqlStatement('DELETE FROM parse_snapshots'))
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
        reopened = replacement.exports()[0].open_space(self.space, ExistingDataset(self.identity.dataset_id))
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
            transaction._transaction.execute(SqlStatement('UPDATE embedding_jobs SET lease_until=:deadline',
                {'deadline': (now + timedelta(seconds=.05)).isoformat()}, expected_rows=1))
        with jobs._tx(*scope[:2]) as transaction:
            execute = transaction._transaction.execute

            def paused(statement):
                result = execute(statement)
                if statement.sql.startswith('SELECT * FROM embedding_jobs'):
                    time.sleep(.1)
                return result

            with patch.object(transaction._transaction, 'execute', side_effect=paused):
                result = transaction.execute(K.JOB_FINISH,
                    ('COMPLETE', [[1., 0.]], 'hash', None, False, None,
                     *scope, 0, row['request_id'], 'RUNNING', 'RUNNING', False))
        self.assertIsNone(result.fetchone())
        self.assertEqual(jobs.status(*scope)[0]['status'], 'RUNNING')

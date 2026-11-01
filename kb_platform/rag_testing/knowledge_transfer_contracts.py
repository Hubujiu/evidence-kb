from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from kb_platform.database_contracts import DatabaseError, Migration, SchemaPackage, SpaceKey, StorageIdentity
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_migrations import migration_connection
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import METADATA, knowledge_schema
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.knowledge_transfer import ARCHIVE, _tables, export_legacy_knowledge, import_knowledge, validate_export, validate_receipt
from kb_platform.session_identity import Actor
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_adapters.structured_transfer import content_hash
from kb_platform.storage_catalog import initialize_configuration
from kb_platform.storage_sdk import ExistingDataset


def legacy_fixture(root):
    path = root / 'legacy.sqlite3'
    key = SpaceKey('legacy-application', 'knowledge', 'content')
    provider = SqlStorageProvider(SQLiteProvider(root, files={key: path}))
    identity = provider.initialize('relational', key)
    archive = json.loads(ARCHIVE.read_text('utf-8'))['engines']['sqlite']
    package = SchemaPackage(key.owner_id, tuple(Migration(item['version'], tuple(item['statements']))
                                               for item in archive['migrations']))
    raw = next(item for item in provider.exports() if item.name == 'relational').open_space(
        key, ExistingDataset(identity.dataset_id))
    raw.prepare(package)
    instant = datetime(2026, 10, 1, 2, 3, 4, 567890, tzinfo=timezone.utc)
    rows = {name: [] for name in METADATA.tables}
    rows.update(tenant_state=[{'tenant_id': 'tenant', 'revision': 7}],
        knowledge_bases=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'name': '原文知识库',
            'owner_id': 'owner', 'revision': 3, 'manifest_id': 'build'}],
        members=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'principal_id': 'owner',
            'capabilities': ['kb.read', 'kb.manage']}],
        sources=[{'tenant_id': 'tenant', 'source_id': 'source', 'owner_id': 'owner',
            'readers': ['Case', 'case', 'case '], 'policy': {'embedding': True}, 'revision': 2}],
        source_versions=[{'tenant_id': 'tenant', 'source_id': 'source', 'source_version_id': 'version',
            'filename': 'fixture.md', 'original_path': 'object:immutable', 'original_sha256': 'a' * 64,
            'media_type': 'text/markdown'}],
        parse_snapshots=[{'tenant_id': 'tenant', 'source_id': 'source', 'source_version_id': 'version',
            'parse_snapshot_id': 'parse', 'document': {'status': 'READY', 'text': '不可变合成原文',
                'evidence_spans': [{'evidence_id': 'span', 'start': 0, 'end': 7}]}}],
        bindings=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'binding_id': 'binding', 'source_id': 'source',
            'source_version_id': 'version', 'parse_snapshot_id': 'parse', 'status': 'ACTIVE', 'revision': 1}],
        builds=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'build_id': 'build', 'expected_manifest_id': None,
            'authority_revision': 7, 'kb_revision': 3, 'index_name': 'old-index',
            'inputs': {'channels': ['bm25', 'dense'], 'saved': 'exact dependency'},
            'status': 'PUBLISHED', 'unit_count': 1}],
        citations=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'citation_id': 'citation', 'build_id': 'build',
            'binding_id': 'binding', 'source_id': 'source', 'source_version_id': 'version',
            'parse_snapshot_id': 'parse', 'element_id': 'element', 'text': '不可变合成原文',
            'evidence_ids': ['span'], 'anchors': [{'offset': 0}], 'table_data': None}],
        embedding_jobs=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'build_id': 'build', 'batch_no': 0,
            'dependency_hash': 'b' * 64, 'provider_hash': 'c' * 64, 'payload': {'units': [{'text': '不可变合成原文'}]},
            'status': 'UNKNOWN', 'attempt': 1, 'request_id': 'physical-unknown', 'lease_until': instant,
            'retry_at': None, 'vectors': [[1.0, 0.0]], 'vectors_hash': 'd' * 64, 'error': 'outcome unknown'}],
        embedding_attempts=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'build_id': 'build', 'batch_no': 0,
            'request_id': 'physical-unknown', 'status': 'UNKNOWN', 'diagnostic': 'do not resend', 'created_at': instant}],
        embedding_circuits=[{'tenant_id': 'tenant', 'kb_id': 'scope', 'provider_hash': 'c' * 64,
            'failures': 1, 'open_until': instant}])
    engine = create_engine('sqlite:///' + path.as_posix())
    with engine.begin() as connection:
        for table in METADATA.sorted_tables:
            for row in rows[table.name]:
                connection.execute(table.insert().values(row))
    provider.close()
    return engine, identity, path


def exercise_legacy_knowledge_transfer(check, fixtures):
    with TemporaryDirectory(dir=fixtures.resource_root) as directory:
        seed_engine, seed_identity, _ = legacy_fixture(Path(directory))
        try:
            seed = export_legacy_knowledge(seed_engine, expected_identity=seed_identity,
                source_format='sqlite-managed-v1', source_frozen=True)
        finally:
            seed_engine.dispose()
    _, source = fixtures.bind('postgresql', 'legacy-knowledge-transfer', owner='knowledge')
    archived = json.loads(ARCHIVE.read_text('utf-8'))['engines']['postgresql']
    migrations = tuple(Migration(row['version'], tuple(row['statements'])) for row in archived['migrations'])
    package = SchemaPackage('knowledge', migrations, **{key: tuple(value) for key, value in archived.items()
                                                        if key.startswith('runtime_')})
    source.prepare(package)
    with migration_connection(source._space) as connection:
        for table in METADATA.sorted_tables:
            for row in _tables(seed['tables'], native=True)[table.name]:
                connection.execute(table.insert().values(row))
    snapshot = export_legacy_knowledge(source._space.migration_engine, expected_identity=source.identity,
        source_format='postgresql-managed-v1', namespace=source.physical_name, source_frozen=True)
    check.assertEqual(snapshot['tables'], seed['tables'])
    outcomes = []
    for engine in ('sqlite', 'postgresql', 'mysql'):
        _, target = fixtures.structured_admin(engine, 'legacy-knowledge-target-' + engine, owner='knowledge')
        target.prepare(knowledge_schema(target.identity.space.owner_id))
        target.seal()
        _, ordinary = fixtures.structured(engine, 'legacy-knowledge-target-' + engine, owner='knowledge')
        ordinary.prepare(knowledge_schema(ordinary.identity.space.owner_id))
        ordinary.seal()
        with check.assertRaisesRegex(DatabaseError, 'INITIALIZED_SQL_TARGET_REQUIRED'):
            import_knowledge(ordinary, snapshot, expected_identity=ordinary.identity, target_offline=True)
        receipt = import_knowledge(target, snapshot, expected_identity=target.identity, target_offline=True)
        check.assertTrue(receipt['republication_required'])
        check.assertFalse(receipt['idempotent'])
        with target.transaction(write=False, scope={'tenant_id': 'tenant', 'kb_id': 'scope'}) as transaction:
            for name in ('sources', 'source_versions', 'parse_snapshots', 'citations', 'embedding_jobs',
                         'embedding_attempts', 'embedding_circuits'):
                actual = transaction.execute(select(METADATA.tables[name])).fetchall()
                check.assertEqual(content_hash(actual), content_hash(snapshot['tables'][name]))
            kb = transaction.execute(select(METADATA.tables['knowledge_bases'])).fetchone()
            check.assertEqual((kb['manifest_id'], kb['revision']), (None, 4))
        repeated = import_knowledge(target, snapshot, expected_identity=target.identity, target_offline=True)
        check.assertTrue(repeated['idempotent'])
        outcomes.append({'source': 'postgresql-legacy-v1', 'target': engine,
            'counts': receipt['counts'], 'publication_reset': True, 'unknown_request_retained': 'physical-unknown',
            'idempotent_readback': True, 'ordinary_handle_admin_import_rejected': True, 'new_remote_calls': 0})
    after = export_legacy_knowledge(source._space.migration_engine, expected_identity=source.identity,
        source_format='postgresql-managed-v1', namespace=source.physical_name, source_frozen=True)
    check.assertEqual(after, snapshot)
    return {'cases': outcomes, 'source_unchanged': True, 'source_migration_hash': migrations[0].digest}


class KnowledgeTransferContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine, self.source, self.path = legacy_fixture(self.root)
        self.addCleanup(self.engine.dispose)
        self.provider = SqlStorageProvider(SQLiteProvider(self.root / 'target'))
        self.addCleanup(self.provider.close)
        key = SpaceKey('new-application', 'knowledge', 'content')
        self.target_identity = self.provider.initialize('structured', key)
        self.target = next(item for item in self.provider.exports() if item.name == 'structured').open_space(
            key, ExistingDataset(self.target_identity.dataset_id))
        self.target.prepare(knowledge_schema(key.owner_id))
        self.target.seal()

    def export(self, **changes):
        return export_legacy_knowledge(self.engine, **{'expected_identity': self.source,
            'source_format': 'sqlite-managed-v1', 'source_frozen': True, **changes})

    def test_exact_unknown_acl_and_evidence_transfer_requires_new_publication(self):
        original = self.path.read_bytes()
        package = self.export()
        self.assertEqual(package, self.export())
        with self.assertRaisesRegex(ValueError, 'SOURCE_MUST_BE_FROZEN'):
            self.export(source_frozen=False)
        receipt = import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        self.assertEqual(validate_receipt(receipt), receipt)
        self.assertFalse(receipt['idempotent'])
        self.assertTrue(receipt['republication_required'])
        self.assertEqual(receipt['publication_reset']['original_pointers'],
                         [{'tenant_id': 'tenant', 'kb_id': 'scope', 'manifest_id': 'build'}])
        reopened = next(item for item in self.provider.exports() if item.name == 'structured').open_space(
            self.target_identity.space, ExistingDataset(self.target_identity.dataset_id))
        repository = KnowledgeRepository(database=reopened)
        with repository.transaction(Actor('tenant', 'owner'), 'scope', write=False) as transaction:
            kb = transaction.execute(K.KB_MEMBERSHIP, ('tenant', 'scope', 'owner')).fetchone()
            self.assertIsNone(kb['manifest_id'])
            self.assertEqual(kb['revision'], 4)
            self.assertEqual(kb['capabilities'], ['kb.read', 'kb.manage'])
            self.assertEqual(transaction.execute(K.AUTHORITY_REVISION, ('tenant',)).fetchone()['revision'], 8)
            self.assertEqual(transaction.execute(K.BUILD_GET, ('tenant', 'scope', 'build')).fetchone()['status'], 'STAGING')
        with self.target.transaction(write=False, scope={'tenant_id': 'tenant', 'kb_id': 'scope'}) as transaction:
            for name in ('sources', 'source_versions', 'parse_snapshots', 'citations', 'embedding_jobs',
                         'embedding_attempts', 'embedding_circuits'):
                rows = transaction.execute(select(METADATA.tables[name])).fetchall()
                self.assertEqual(content_hash(rows), content_hash(package['tables'][name]))
        repeated = import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        self.assertTrue(repeated['idempotent'])
        self.assertEqual(repeated['target_records_sha256'], receipt['target_records_sha256'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_exports_wrong_identities_and_online_target_fail_closed(self):
        package = self.export()
        changed = deepcopy(package)
        changed['tables']['embedding_jobs'][0]['status'] = 'COMPLETE'
        with self.assertRaisesRegex(ValueError, 'TRANSFER_INTEGRITY'):
            validate_export(changed)
        with self.assertRaisesRegex(DatabaseError, 'IDENTITY_MISMATCH'):
            self.export(expected_identity=StorageIdentity(self.source.space, 'wrong'))
        with self.assertRaisesRegex(ValueError, 'TARGET_MUST_BE_OFFLINE'):
            import_knowledge(self.target, package, expected_identity=self.target_identity)
        with self.assertRaisesRegex(DatabaseError, 'INITIALIZED_SQL_TARGET_REQUIRED'):
            import_knowledge(self.target, package,
                expected_identity=StorageIdentity(self.target_identity.space, 'wrong'), target_offline=True)
        receipt = import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        receipt['counts']['embedding_jobs'] = 0
        with self.assertRaisesRegex(ValueError, 'RECEIPT_INTEGRITY'):
            validate_receipt(receipt)

    def test_failed_import_rolls_back_all_domain_rows_and_repeat_cannot_overwrite_changes(self):
        package = self.export()
        broken = deepcopy(package)
        broken['tables']['citations'][0]['source_version_id'] = 'missing'
        broken['sha256'] = content_hash({key: value for key, value in broken.items() if key != 'sha256'})
        with self.assertRaises(IntegrityError):
            import_knowledge(self.target, broken, expected_identity=self.target_identity, target_offline=True)
        with self.target.transaction(write=False, scope={'tenant_id': 'tenant', 'kb_id': 'scope'}) as transaction:
            for table in METADATA.sorted_tables:
                self.assertEqual(transaction.execute(select(table)).fetchall(), [])
        import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        with self.target.transaction(scope={'tenant_id': 'tenant', 'kb_id': 'scope'}) as transaction:
            transaction.execute(METADATA.tables['knowledge_bases'].update().values(name='new work'))
        with self.assertRaisesRegex(DatabaseError, 'TRANSFER_TARGET_CHANGED'):
            import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)

    def test_cli_explicit_export_and_import_use_initialized_v2_identity(self):
        from scripts.migrate_knowledge_storage import main

        output, imported = self.root / 'export-command', self.root / 'import-command'
        configuration = initialize_configuration({'version': 2, 'application_id': 'cli-application',
            'instances': {'main': {'provider': 'sqlite', 'config': {'root': str(self.root / 'cli-target')}}},
            'services': {'records': {'instance': 'main', 'export': 'structured'}},
            'bindings': {'repository': {'primary': {'service': 'records', 'owner': 'knowledge', 'space': 'content'}}}})
        path = self.root / 'storage.json'
        path.write_text(json.dumps(configuration), encoding='utf-8')
        with patch.dict('os.environ', {'KB_TRANSFER_SOURCE': 'sqlite:///' + self.path.as_posix()}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['export', '--source-url-env', 'KB_TRANSFER_SOURCE', '--source-format',
                'sqlite-managed-v1', '--source-application', self.source.space.application_id, '--source-owner',
                self.source.space.owner_id, '--source-space', self.source.space.space_id, '--expected-source-dataset',
                self.source.dataset_id, '--source-frozen', '--output', str(output)]), 0)
            self.assertEqual(main(['import', '--input', str(output / 'knowledge-export.json'), '--target-config',
                str(path), '--expected-target-dataset', configuration['bindings']['repository']['primary']['dataset_id'],
                '--target-offline', '--output', str(imported)]), 0)
        receipt = validate_receipt(json.loads((imported / 'receipt.json').read_text('utf-8')))
        self.assertEqual(receipt['counts']['embedding_jobs'], 1)
        self.assertTrue(receipt['republication_required'])

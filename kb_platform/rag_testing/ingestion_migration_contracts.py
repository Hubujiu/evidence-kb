from copy import deepcopy
from hashlib import sha256
import io
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.sql.dml import Insert

from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.database_migrations import migration_connection
from kb_platform.documents import parse_native_document
from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import METADATA
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.knowledge_transfer import export_legacy_knowledge, import_knowledge
from kb_platform.storage import OriginalStore
from kb_platform.storage_adapters.structured_transfer import _RECEIPTS
from kb_platform.storage_catalog import initialize_configuration
from kb_platform import storage_reset

from .formal_state_machines import registry_violations, source_digest
from .ingestion_migration_rules import ingestion_migration_violations


ROOT = Path(__file__).resolve().parents[2]


class IngestionAdmissionContracts(unittest.TestCase):
    def setUp(self):
        from .knowledge_persistence_contracts import KnowledgePersistenceContracts
        KnowledgePersistenceContracts.setUp(self)
        self.search = EmbeddedSearch(self.root / 'search')
        self.addCleanup(self.search.close)
        self.originals = OriginalStore(self.root / 'originals')
        self.addCleanup(self.originals.close)
        self.repository = KnowledgeRepository(database=self.database)
        self.platform = Platform(repository=self.repository, search=self.search, store=self.originals,
                                 parser=parse_native_document, embedding_jobs=None)
        self.actor = Actor('synthetic-ingestion', 'owner')

    def test_real_parse_outcomes_gate_builds_and_preserve_original_identity(self):
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'white').save(stream, format='PNG')
        cases = [('READY', 'source.txt', b'Immutable source evidence for a lexical build.', {}),
                 ('PARTIAL', 'scan.png', stream.getvalue(), {'ocr': True}),
                 ('PARTIAL', 'source.html', b'<p>Original retained evidence.</p><img src="unfetched.png">', {}),
                 ('BLOCKED', 'source.txt', b'Blocked native processing.', {'native_parse': False}),
                 ('FAILED', 'source.unsupported', b'Unsupported source format.', {})]
        for expected, filename, original, policy in cases:
            with self.subTest(outcome=expected):
                scope = self.platform.create_kb(self.actor, expected)['kb_id']
                result = self.platform.ingest(self.actor, scope, filename, original, policy=policy)
                digest = sha256(original).hexdigest()
                self.assertEqual(result['document']['status'], expected)
                self.assertEqual(result['source_version_id'], digest)
                self.assertEqual(result['document']['original_sha256'], digest)
                if filename == 'source.html':
                    self.assertTrue(result['document']['elements'])
                    bindings = self.platform._source_snapshot(self.actor, scope)['rows']
                    units, _ = self.platform._units(self.actor, scope, 'synthetic-uncommitted-build', bindings)
                    self.assertTrue(units)
                with self.database.transaction(write=False, scope={'tenant_id': self.actor.tenant_id,
                                                                   'kb_id': scope}) as transaction:
                    row = transaction.execute(select(METADATA.tables['source_versions']).where(
                        METADATA.tables['source_versions'].c.source_id == result['source_id'])).fetchone()
                self.assertEqual(self.originals.read(row['original_path'], self.actor.tenant_id, digest), original)
                with patch.object(self.search, 'stage', wraps=self.search.stage) as stage:
                    if expected == 'READY':
                        built = self.platform.stage(self.actor, scope, ['bm25'])
                        self.assertEqual(built['status'], 'VALIDATED')
                        self.assertEqual(built['model_admission'], 'NOT_ENABLED')
                        self.assertGreater(built['unit_count'], 0)
                        self.assertEqual(stage.call_count, 1)
                    else:
                        with self.assertRaises(PlatformError) as rejected:
                            self.platform.stage(self.actor, scope, ['bm25'])
                        self.assertEqual(rejected.exception.code, 'INCOMPLETE_PARSE')
                        stage.assert_not_called()
                        with self.database.transaction(write=False, scope={'tenant_id': self.actor.tenant_id,
                                                                           'kb_id': scope}) as transaction:
                            self.assertEqual(transaction.execute(select(METADATA.tables['builds'])).fetchall(), [])
                with self.assertRaises(PlatformError):
                    self.platform.stage(Actor('other-tenant', self.actor.principal_id), scope, ['bm25'])

    def test_policy_change_during_real_parse_rejects_stale_binding_not_original_storage(self):
        scope = self.platform.create_kb(self.actor, 'Policy race')['kb_id']
        previous = self.platform.ingest(self.actor, scope, 'old.txt', b'Old immutable evidence.')
        replacement = b'New bytes parsed with the old processing policy.'

        def parse_after_policy_change(data, filename, policy):
            self.platform.set_source_policy(self.actor, previous['source_id'], {'native_parse': False})
            return parse_native_document(data, filename, policy)

        self.platform.parser = parse_after_policy_change
        stored, put = [], self.originals.put

        def observe_original(tenant, data):
            result = put(tenant, data)
            stored.append(result)
            return result

        with patch.object(self.originals, 'put', side_effect=observe_original):
            with self.assertRaises(PlatformError) as rejected:
                self.platform.ingest(self.actor, scope, 'new.txt', replacement, source_id=previous['source_id'])
        self.assertEqual(rejected.exception.code, 'STALE_PARSE')
        with self.repository.transaction(self.actor, scope, write=False) as transaction:
            rows = transaction.execute(K.BINDINGS_ACTIVE, (self.actor.tenant_id, scope)).fetchall()
        with self.database.transaction(write=False, scope={'tenant_id': self.actor.tenant_id,
                                                           'kb_id': scope}) as transaction:
            versions = transaction.execute(select(METADATA.tables['source_versions'])).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_version_id'], previous['source_version_id'])
        self.assertEqual([row['source_version_id'] for row in versions], [previous['source_version_id']])
        self.assertEqual(len(stored), 1)
        locator, digest = stored[0]
        self.assertEqual(digest, sha256(replacement).hexdigest())
        self.assertEqual(self.originals.read(locator, self.actor.tenant_id, digest), replacement)


class MigrationAtomicityContracts(unittest.TestCase):
    def test_failure_after_receipt_insert_rolls_back_data_then_retry_commits_once(self):
        from .knowledge_transfer_contracts import KnowledgeTransferContracts
        KnowledgeTransferContracts.setUp(self)
        package = export_legacy_knowledge(self.engine, expected_identity=self.source,
                                         source_format='sqlite-managed-v1', source_frozen=True)
        original = self.path.read_bytes()
        execute = Connection.execute

        def fail_after_receipt(connection, statement, *args, **kwargs):
            result = execute(connection, statement, *args, **kwargs)
            if isinstance(statement, Insert) and statement.table.name == _RECEIPTS.name:
                raise RuntimeError('synthetic failure after receipt insertion before commit')
            return result

        with patch.object(Connection, 'execute', fail_after_receipt):
            with self.assertRaisesRegex(RuntimeError, 'after receipt insertion'):
                import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        with migration_connection(self.target._space) as connection:
            for table in METADATA.sorted_tables:
                self.assertEqual(connection.execute(select(table)).all(), [])
            self.assertEqual(connection.execute(select(_RECEIPTS)).all(), [])
        receipt = import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        self.assertFalse(receipt['idempotent'])
        repeated = import_knowledge(self.target, package, expected_identity=self.target_identity, target_offline=True)
        self.assertTrue(repeated['idempotent'])
        with migration_connection(self.target._space) as connection:
            self.assertEqual(len(connection.execute(select(_RECEIPTS)).all()), 1)
            scopes = connection.execute(select(METADATA.tables['knowledge_bases'])).mappings().all()
            self.assertTrue(scopes)
            self.assertTrue(all(row['manifest_id'] is None for row in scopes))
            jobs = connection.execute(select(METADATA.tables['embedding_jobs'])).mappings().all()
            self.assertEqual([row['status'] for row in jobs], ['UNKNOWN'])
        self.assertEqual(self.target.identity, self.target_identity)
        self.assertNotEqual(self.target_identity.dataset_id, self.source.dataset_id)
        self.assertEqual(self.path.read_bytes(), original)

    def test_two_real_resets_publish_one_complete_config_without_replacing_old_identities(self):
        from .storage_reset_contracts import configuration, document, providers
        with TemporaryDirectory() as temporary:
            root, catalog = Path(temporary), providers()
            settings = configuration(root)
            settings['bindings']['identity'] = {
                'data': {'service': 'financial', 'owner': 'identity', 'space': 'access'}}
            initial = initialize_configuration(settings, provider_catalog=catalog)
            for consumer in initial['bindings']:
                document(initial, consumer, catalog, value={'reserved': 23, 'outcome': 'UNKNOWN'})
            source, output = root / 'source.json', root / 'winner.json'
            original = json.dumps(initial).encode()
            source.write_bytes(original)
            real_link = storage_reset.os.link
            winner = []

            def second_wins(temporary_file, destination):
                with patch.object(storage_reset.os, 'link', real_link):
                    winner.append(storage_reset.reset_to_file(source, output, provider_catalog=catalog))
                real_link(temporary_file, destination)

            with patch.object(storage_reset.os, 'link', second_wins):
                with self.assertRaises(FileExistsError):
                    storage_reset.reset_to_file(source, output, provider_catalog=catalog)
            self.assertEqual(len(winner), 1)
            self.assertEqual(winner[0]['status'], 'COMPLETE')
            published = json.loads(output.read_bytes())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(published['bindings']['provider'], initial['bindings']['provider'])
            self.assertEqual(published['bindings']['identity'], initial['bindings']['identity'])
            for consumer in initial['bindings']:
                self.assertEqual(document(initial, consumer, catalog).value,
                                 {'reserved': 23, 'outcome': 'UNKNOWN'})
                if consumer not in {'provider', 'identity'}:
                    self.assertIsNone(document(published, consumer, catalog))
                    self.assertNotEqual(published['bindings'][consumer], initial['bindings'][consumer])
            self.assertEqual(list(root.glob('.storage-reset-*')), [])


class IngestionMigrationLintContracts(unittest.TestCase):
    def test_semantic_guards_survive_source_hash_refresh(self):
        self.assertEqual(ingestion_migration_violations(ROOT), [])
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        reviewed = next(row for row in registry['machines'] if row['id'] == 'ingestion_migration')
        transfer = (ROOT / 'kb_platform/knowledge_transfer.py').read_text('utf-8')
        pointer_reset = "row['manifest_id'], row['revision'] = None, row['revision'] + 1"
        late_reset = transfer.replace(pointer_reset, 'pass').replace(
            "    body = {**receipt, 'idempotent': repeated}",
            "    for row in transformed['knowledge_bases']:\n        " + pointer_reset +
            "\n    body = {**receipt, 'idempotent': repeated}")
        mutations = [
            ('kb_platform/core.py', "row['document']['status'] != 'READY'", "row['document']['status'] == 'FAILED'"),
            ('kb_platform/core.py', 'if not units:', 'if False:'),
            ('kb_platform/core.py', "document.get('original_sha256') != digest", "document.get('original_sha256') == digest"),
            ('kb_platform/core.py', "current['revision'] != existing['revision']", 'False'),
            ('kb_platform/knowledge_transfer.py', "row['manifest_id'], row['revision'] = None,", "row['manifest_id'], row['revision'] = 'copied-manifest',"),
            ('kb_platform/knowledge_transfer.py', transfer, late_reset),
            ('kb_platform/storage_adapters/structured_transfer.py', '        connection.execute(_RECEIPTS.insert()', '    connection.execute(_RECEIPTS.insert()'),
            ('kb_platform/storage_adapters/structured_transfer.py', "rows[0]['dataset_id'] != expected.dataset_id", 'False'),
            ('kb_platform/storage_reset.py', 'os.link(temporary, output)', 'os.replace(temporary, output)'),
            ('kb_platform/storage_reset.py', 'if role in BUSINESS_ROLES)', 'if role in BUSINESS_ROLES | PRESERVED_ROLES)'),
        ]
        for relative, before, after in mutations:
            with self.subTest(path=relative, mutation=before[:60]), TemporaryDirectory() as directory:
                root = Path(directory)
                machine = deepcopy(reviewed)
                for path in machine['owners']:
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / path, target)
                target = root / relative
                original = target.read_text('utf-8')
                self.assertIn(before, original)
                target.write_text(original.replace(before, after), 'utf-8')
                for binding in machine['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                for model in machine['models'] + machine['negative_controls']:
                    for key in ('module', 'config'):
                        target = root / model[key]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(ROOT / model[key], target)
                document = {'schema_version': 1, 'machines': [machine],
                            'discovery': {'roots': ['kb_platform'], 'exclusions': []}}
                errors = registry_violations(root, document)
                self.assertFalse(any('source review drift' in error for error in errors), errors)
                self.assertTrue(any(error.startswith('ingestion_migration:') for error in errors), errors)

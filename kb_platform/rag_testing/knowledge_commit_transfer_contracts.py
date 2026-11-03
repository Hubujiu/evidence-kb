from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.database_contracts import SpaceKey
from kb_platform.knowledge_commit_transfer import export_authority, import_authority_sessions
from kb_platform.knowledge_schema import LEGACY_METADATA, knowledge_schema
from kb_platform.persistence_local import local_space
from kb_platform.session_identity import Actor
from kb_platform.session_store import SessionStore
from kb_platform.session_transfer import export_sessions, export_frozen_root
from kb_platform.storage_sdk.relational import StructuredSchema


class KnowledgeCommitTransferContracts(unittest.TestCase):
    def test_frozen_pair_preserves_completed_running_requests_artifacts_and_authority(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source, source_service = local_space(root / 'old-authority', SpaceKey('app', 'authority', 'primary'))
        self.addCleanup(source_service.close)
        source.prepare(StructuredSchema('authority', LEGACY_METADATA,
            appendonly=('source_versions', 'parse_snapshots', 'citations')))
        source.seal()
        with source.transaction(scope={'tenant_id': 'tenant', 'kb_id': 'scope'}) as tx:
            tx.insert(LEGACY_METADATA.tables['tenant_state'], {'tenant_id': 'tenant', 'revision': 7})
            tx.insert(LEGACY_METADATA.tables['knowledge_bases'], {'tenant_id': 'tenant', 'kb_id': 'scope',
                'name': 'Synthetic migration', 'owner_id': 'owner', 'revision': 3, 'manifest_id': None})
            tx.insert(LEGACY_METADATA.tables['members'], {'tenant_id': 'tenant', 'kb_id': 'scope',
                'principal_id': 'owner', 'capabilities': ['kb.read']})
        old = SessionStore(root / 'old-sessions')
        self.addCleanup(old.close)
        actor = Actor('tenant', 'owner')
        completed = old.create(actor, 'scope', {'retained': True})['session_id']
        token = old.begin(actor, completed, 'complete', 0, 'fingerprint')['token']
        artifact = old.artifact(actor, completed, token, {'immutable': 'evidence'})
        response = old.finish(actor, completed, token, {'retained': True}, {'answer': 'Synthetic answer'})
        pending = old.create(actor, 'scope', {})['session_id']
        lease = old.begin(actor, pending, 'pending', 0, 'pending-fingerprint')
        authority = export_authority(source._space.migration_engine, expected_identity=source.identity, source_frozen=True)
        sessions = export_sessions(old, source_frozen=True)
        old.close()
        frozen_bytes = {path.relative_to(root / 'old-sessions'): path.read_bytes()
            for path in (root / 'old-sessions').rglob('*') if path.is_file()}
        self.assertEqual(export_frozen_root(root / 'old-sessions', source_frozen=True), sessions)
        self.assertEqual({path.relative_to(root / 'old-sessions'): path.read_bytes()
            for path in (root / 'old-sessions').rglob('*') if path.is_file()}, frozen_bytes)
        target = SessionStore(root / 'new-composite', schema=knowledge_schema)
        self.addCleanup(target.close)
        receipt = import_authority_sessions(target.database, authority, sessions,
            expected_identity=target.identity, target_offline=True)
        self.assertFalse(receipt['idempotent'])
        self.assertTrue(import_authority_sessions(target.database, authority, sessions,
            expected_identity=target.identity, target_offline=True)['idempotent'])
        self.assertEqual(export_sessions(target, source_frozen=True)['tables'], sessions['tables'])
        self.assertEqual(target.begin(actor, completed, 'complete', 0, 'fingerprint'), {'cached': response})
        self.assertEqual(target.read_artifact(actor, completed, artifact['artifact_id']), {'immutable': 'evidence'})
        with target.database.transaction(write=False) as tx:
            from sqlalchemy import select

            row = tx.execute(select(target._tables['requests']).where(
                target._tables['requests'].c.session == pending)).fetchone()
        self.assertEqual(row['token'], lease['token'])
        self.assertEqual(row['status'], 'running')
        self.assertEqual(export_frozen_root(root / 'old-sessions', source_frozen=True), sessions)
        self.assertEqual(export_authority(source._space.migration_engine,
            expected_identity=source.identity, source_frozen=True), authority)
        damaged = deepcopy(authority)
        damaged['tables']['tenant_state'][0]['revision'] = 0
        with self.assertRaisesRegex(ValueError, 'INTEGRITY'):
            import_authority_sessions(target.database, damaged, sessions,
                expected_identity=target.identity, target_offline=True)
        with self.assertRaisesRegex(ValueError, 'OFFLINE'):
            import_authority_sessions(target.database, authority, sessions, expected_identity=target.identity)

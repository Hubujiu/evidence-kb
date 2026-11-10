from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.database_contracts import DatabaseError, Migration, SchemaPackage
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.storage_sdk import (
    EphemeralCacheSpace, KV_V1, SpaceKey, StorageError, StorageExport, StorageIdentity,
)
from kb_platform.storage_service import StorageService


class _Cache:
    def __init__(self, key, label):
        self.identity = StorageIdentity(key, 'cache')
        self.label, self.calls = label, 0

    def get(self, key):
        self.calls += 1
        return self.label.encode()

    def set(self, key, value):
        self.calls += 1

    def delete(self, key):
        self.calls += 1
        return True


class StorageGrantGenerationContracts(unittest.TestCase):
    def test_revoked_storage_handle_cannot_reenter_same_registration_under_new_owner(self):
        service, key = StorageService(), SpaceKey('app', 'owner', 'data')
        self.addCleanup(service.close)
        old, new = _Cache(key, 'old'), _Cache(key, 'new')
        def export(item):
            return StorageExport('kv', (KV_V1,), lambda key, target: item)
        dispose = service.register('cache', export(old), registration_id='same', owner_identity={'generation': 1})
        grant = service.grant('cache', KV_V1, key, 'consumer', target=EphemeralCacheSpace('cache'))
        bound = service.bind(grant)
        dispose()
        service.register('cache', export(new), registration_id='same', owner_identity={'generation': 2})
        with self.assertRaises(StorageError):
            bound.get('key')
        self.assertEqual((old.calls, new.calls), (0, 0))
        current = service.bind(service.grant('cache', KV_V1, key, 'consumer', target=EphemeralCacheSpace('cache')))
        self.assertEqual(current.get('key'), b'new')
        self.assertEqual((old.calls, new.calls), (0, 1))

    def test_revoked_database_handle_cannot_write_old_dataset_using_new_owner_lease(self):
        with TemporaryDirectory() as directory:
            service, key = DatabaseService(), SpaceKey('app', 'tasks', 'data')
            old, new = SQLiteProvider(Path(directory) / 'old'), SQLiteProvider(Path(directory) / 'new')
            try:
                dispose = service.register('db', old, registration_id='same', owner_identity={'generation': 1}, owned=False)
                grant = service.grant('db', key, 'consumer')
                bound = service.bind(grant)
                bound.prepare(SchemaPackage('tasks', (Migration(1, ('CREATE TABLE tasks(id TEXT PRIMARY KEY)',)),)))
                bound.seal()
                dispose()
                service.register('db', new, registration_id='same', owner_identity={'generation': 2}, owned=False)
                with self.assertRaises(DatabaseError):
                    with bound.transaction() as tx:
                        tx.execute("INSERT INTO tasks VALUES('stale')")
                current = service.bind(service.grant('db', key, 'consumer'))
                self.assertNotEqual(current.identity.dataset_id, bound.identity.dataset_id)
                service.register('inspect', old, registration_id='fresh', owned=False)
                inspect = service.bind(service.grant('inspect', key, 'auditor'))
                with inspect.transaction(write=False) as tx:
                    self.assertEqual(tx.execute('SELECT count(*) FROM tasks').scalar(), 0)
            finally:
                service.close()
                old.close()
                new.close()

    def test_storage_owner_callback_cannot_replace_entry_between_lease_checks(self):
        self._owner_callback(StorageService, StorageError)

    def test_database_owner_callback_cannot_replace_entry_between_lease_checks(self):
        self._owner_callback(DatabaseService, DatabaseError)

    def _owner_callback(self, factory, error_type):
        service, key, actions = factory(), SpaceKey('app', 'owner', 'data'), []
        if factory is StorageService:
            old, new = _Cache(key, 'old'), _Cache(key, 'new')
            old = StorageExport('kv', (KV_V1,), lambda key, target, item=old: item)
            new = StorageExport('kv', (KV_V1,), lambda key, target, item=new: item)
            options = {}
        else:
            old = type('Adapter', (), {'open_space': lambda self, key: None})()
            new = type('Adapter', (), {'open_space': lambda self, key: None})()
            options = {'owned': False}

        @contextmanager
        def ownership():
            actions.append('enter-old-owner')
            dispose()
            service.register('route', new, registration_id='same', owner_identity={'generation': 2}, **options)
            try:
                yield
            finally:
                actions.append('exit-old-owner')

        dispose = service.register('route', old, registration_id='same', owner_identity={'generation': 1},
                                   owner_lease=ownership, **options)
        try:
            with self.assertRaises(error_type), service.lease('route', 'same'):
                self.fail('old ownership context admitted a replacement entry')
            self.assertEqual(actions, ['enter-old-owner', 'exit-old-owner'])
            self.assertEqual(service.inspect()['route']['active_calls'], 0)
            with service.lease('route', 'same') as actual:
                self.assertIs(actual, new)
        finally:
            service.close()


class StorageGrantLintContracts(unittest.TestCase):
    def test_storage_review_is_mandatory_for_its_registered_owner(self):
        import json
        from unittest.mock import patch
        from kb_platform.rag_testing.formal_state_machines import registry_violations

        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'formal/registry-v1.json').read_text())
        self.assertIn('storage_registry', {item['id'] for item in registry['machines']})
        with patch('scripts.storage_grant_checks.violations', return_value=['LEASE_GUARD_PROBE']) as check:
            self.assertIn('LEASE_GUARD_PROBE', registry_violations(root, registry))
            check.assert_called_once_with(root)

    def test_mandatory_lease_review_rejects_bypass_even_without_source_hash_checks(self):
        from scripts.storage_grant_checks import violations

        root = Path(__file__).resolve().parents[2]
        self.assertEqual(violations(root), [])
        changes = (
            (' is not entry:', ' != entry:'),
            (' is not route:', ' != route:'),
            ('self._grants.get(grant.token) is not grant', 'self._grants.get(grant.token) != grant'),
            ('self._validate_lease_grant(grant, name, registration)', 'pass'),
            ('grant=grant) as', 'grant=None) as'),
            ('grant=self._grant):', 'grant=None):'),
            ('.active += 1', '.active += 0'),
            ('.active -= 1', '.active -= 0'),
        )
        with TemporaryDirectory() as directory:
            target = Path(directory)
            (target / 'kb_platform').mkdir()
            originals = {name: (root / 'kb_platform' / name).read_text() for name in (
                'storage_service.py', 'database_service.py')}
            for name, source in originals.items():
                for before, after in changes:
                    if before not in source:
                        continue
                    with self.subTest(name=name, mutation=before):
                        for file, text in originals.items():
                            (target / 'kb_platform' / file).write_text(text)
                        (target / 'kb_platform' / name).write_text(source.replace(before, after, 1))
                        self.assertTrue(violations(target))

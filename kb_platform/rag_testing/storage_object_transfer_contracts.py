from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.storage_catalog import default_storage_providers
from kb_platform.storage_object_transfer import MAX_TRANSFER_BYTES, MAX_TRANSFER_RECORDS, transfer_objects
from kb_platform.storage_sdk import ExistingDataset, OBJECT_V1, SpaceKey, StorageError
from kb_platform.storage_service import StorageService


class StorageObjectTransferContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = default_storage_providers()
        self.source_provider, self.source = self.open('source')
        self.target_provider, self.target = self.open('target')
        self.data = b'\x00\xff original bytes'
        self.ref = self.source.put('tenant', self.data)
        self.records = [{'tenant_id': 'tenant', 'locator': self.ref.locator, 'sha256': self.ref.sha256}]

    def open(self, name, dataset_id=None):
        provider = self.catalog['files'].factory({'root': str(self.root / name)})
        self.addCleanup(provider.close)
        space = SpaceKey('transfer-contract', 'originals', name)
        if dataset_id is None:
            dataset_id = provider.initialize('object', space).dataset_id
        registry = StorageService()
        self.addCleanup(registry.close)
        registry.register(name, provider.exports()[0])
        grant = registry.grant(name, OBJECT_V1, space, 'transfer', target=ExistingDataset(dataset_id))
        return provider, registry.bind(grant)

    def transfer(self, records=None, **kwargs):
        return transfer_objects(self.source, self.target, self.records if records is None else records,
            expected_source_dataset_id=kwargs.get('source_id', self.source.identity.dataset_id),
            expected_target_dataset_id=kwargs.get('target_id', self.target.identity.dataset_id))

    def target_objects(self):
        return [path for path in (self.root / 'target').rglob('*')
                if path.is_file() and '.storage-identities' not in path.parts]

    def test_verified_mapping_preserves_source_binary_content_and_repeats_idempotently(self):
        empty = self.source.put('tenant', b'')
        other = self.source.put('other-tenant', self.data)
        self.records.extend([{'tenant_id': 'tenant', 'locator': empty.locator, 'sha256': empty.sha256},
                             {'tenant_id': 'other-tenant', 'locator': other.locator, 'sha256': other.sha256}])
        original = deepcopy(self.records)
        result = self.transfer()
        self.assertEqual(self.records, original)
        self.assertEqual(result['source_identity']['dataset_id'], self.source.identity.dataset_id)
        self.assertEqual(result['target_identity']['dataset_id'], self.target.identity.dataset_id)
        self.assertEqual(len(result['records']), 3)
        self.assertEqual(len(self.target_objects()), 3)
        for row, data in zip(result['records'], (self.data, b'', self.data), strict=True):
            self.assertNotEqual(row['source_locator'], row['target_locator'])
            self.assertEqual(row['byte_length'], len(data))
            self.assertEqual(self.source.read(row['tenant_id'], row['source_locator'], sha256=row['sha256']), data)
            self.assertEqual(self.target.read(row['tenant_id'], row['target_locator'], sha256=row['sha256']), data)
        self.assertEqual(self.transfer(), result)
        self.assertEqual(len(self.target_objects()), 3)

    def test_reopened_target_uses_explicit_identity_and_mapping_remains_valid(self):
        result = self.transfer()
        self.target_provider.close()
        _, restored = self.open('target', self.target.identity.dataset_id)
        row = result['records'][0]
        self.assertEqual(restored.identity, self.target.identity)
        self.assertEqual(restored.read('tenant', row['target_locator'], sha256=row['sha256']), self.data)

    def test_wrong_expected_identity_rejects_before_any_target_object_is_written(self):
        for changed in ({'source_id': 'other-source'}, {'target_id': 'other-target'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(StorageError,
                    'STORAGE_DATASET_IDENTITY_MISMATCH'):
                self.transfer(**changed)
            self.assertEqual(self.target_objects(), [])
        self.assertEqual(self.source.read('tenant', self.ref.locator), self.data)

    def test_invalid_and_foreign_records_are_all_rejected_before_copying_valid_prefix(self):
        foreign_ref = self.target.put('tenant', b'foreign')
        invalid = ({'tenant_id': 'tenant', 'locator': self.ref.locator, 'sha256': 'bad'},
                   {**self.records[0], 'unexpected': True}, {**self.records[0], 'tenant_id': ''})
        for record in invalid:
            with self.subTest(record=record), self.assertRaisesRegex(StorageError,
                    'STORAGE_OBJECT_TRANSFER_RECORD_INVALID'):
                self.transfer([self.records[0], record])
        for record in ({**self.records[0], 'tenant_id': 'intruder'},
                       {**self.records[0], 'sha256': '0' * 64},
                       {'tenant_id': 'tenant', 'locator': foreign_ref.locator, 'sha256': foreign_ref.sha256}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.transfer([self.records[0], record])
        self.assertEqual(len(self.target_objects()), 1)
        self.assertEqual(self.target.read('tenant', foreign_ref.locator), b'foreign')

    def test_record_count_and_declared_byte_limits_reject_before_storage_reads_or_writes(self):
        for invalid in ([], {}, [self.records[0]] * (MAX_TRANSFER_RECORDS + 1)):
            with self.subTest(kind=type(invalid).__name__), self.assertRaisesRegex(StorageError,
                    'STORAGE_OBJECT_TRANSFER_LIMIT'):
                self.transfer(invalid)
        oversized = replace(self.ref, size=64 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_TRANSFER_LIMIT'):
            self.transfer([{**self.records[0], 'locator': oversized.locator}])
        declared = replace(self.ref, size=64 * 1024 * 1024)
        repeated = [{**self.records[0], 'locator': declared.locator}] * (MAX_TRANSFER_BYTES // declared.size + 1)
        with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_TRANSFER_LIMIT'):
            self.transfer(repeated)
        self.assertEqual(self.target_objects(), [])

    def test_corrupt_source_is_not_copied_and_remains_available_for_diagnosis(self):
        path = (self.root / 'source').joinpath(*self.ref.object_key.split('/'))
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            self.transfer()
        self.assertEqual(path.read_bytes(), b'corrupt')
        self.assertEqual(self.target_objects(), [])

    def test_corrupt_existing_target_is_not_overwritten_on_retry(self):
        result = self.transfer()
        ref = self.target.reference('tenant', result['records'][0]['target_locator'])
        path = (self.root / 'target').joinpath(*ref.object_key.split('/'))
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            self.transfer()
        self.assertEqual(path.read_bytes(), b'corrupt')
        self.assertEqual(self.source.read('tenant', self.ref.locator), self.data)

    def test_target_is_read_back_before_a_verified_mapping_is_returned(self):
        target, root = self.target, self.root / 'target'

        class CorruptAfterPut:
            def __getattr__(self, name):
                return getattr(target, name)

            def put(self, tenant_id, data):
                ref = target.put(tenant_id, data)
                root.joinpath(*ref.object_key.split('/')).write_bytes(b'corrupt after acknowledged write')
                return ref

        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            transfer_objects(self.source, CorruptAfterPut(), self.records,
                expected_source_dataset_id=self.source.identity.dataset_id,
                expected_target_dataset_id=target.identity.dataset_id)
        self.assertEqual(self.source.read('tenant', self.ref.locator), self.data)


if __name__ == '__main__':
    unittest.main()

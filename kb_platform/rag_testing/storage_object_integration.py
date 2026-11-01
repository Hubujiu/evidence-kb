from dataclasses import asdict
import unittest
from uuid import uuid4

from kb_platform.storage_catalog import default_storage_providers
from kb_platform.storage_object_transfer import transfer_objects
from kb_platform.storage_sdk import ExistingDataset, OBJECT_V1, SpaceKey, StorageError
from kb_platform.storage_service import StorageService


def suite(fixtures):
    class StorageObjectIntegration(unittest.TestCase):
        def setUp(self):
            import boto3
            from botocore.config import Config

            self.catalog = default_storage_providers()
            self.client = boto3.client('s3', endpoint_url=fixtures.selected['s3_endpoint'],
                aws_access_key_id=fixtures.environment['KB_STORAGE_TEST_S3_ACCESS_KEY'],
                aws_secret_access_key=fixtures.environment['KB_STORAGE_TEST_S3_SECRET_KEY'],
                region_name='us-east-1', config=Config(retries={'total_max_attempts': 1},
                    s3={'addressing_style': 'path'}, connect_timeout=5, read_timeout=20))
            self.bucket = 'storage-transfer-' + uuid4().hex
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except BaseException:
                self.client.close()
                raise
            fixtures.buckets.append((self.client, self.bucket))
            self.scope = fixtures.run_id + '-' + uuid4().hex
            self.configs = {
                'files': {'root': str(fixtures.resource_root / 'object-transfer' / self.scope)},
                's3': {'endpoint_url': fixtures.selected['s3_endpoint'], 'bucket': self.bucket,
                       'access_key': fixtures.environment['KB_STORAGE_TEST_S3_ACCESS_KEY'],
                       'secret_key': fixtures.environment['KB_STORAGE_TEST_S3_SECRET_KEY'],
                       'prefix': 'transfer/' + self.scope}}

        def open(self, backend, name, dataset_id=None):
            provider = self.catalog[backend].factory(self.configs[backend])
            self.addCleanup(provider.close)
            space = SpaceKey('storage-acceptance-' + fixtures.run_id, 'object-transfer', self.scope + '-' + name)
            if dataset_id is None:
                dataset_id = provider.initialize('object', space).dataset_id
            registry = StorageService()
            self.addCleanup(registry.close)
            registry.register('objects', provider.exports()[0])
            grant = registry.grant('objects', OBJECT_V1, space, 'transfer-acceptance',
                                   target=ExistingDataset(dataset_id))
            return provider, registry.bind(grant)

        def transfer(self, source, target, records, **changed):
            return transfer_objects(source, target, records,
                expected_source_dataset_id=changed.get('source_id', source.identity.dataset_id),
                expected_target_dataset_id=changed.get('target_id', target.identity.dataset_id))

        def corrupt(self, backend, ref):
            config = self.configs[backend]
            if backend == 'files':
                from pathlib import Path

                Path(config['root']).joinpath(*ref.object_key.split('/')).write_bytes(b'corrupt')
            else:
                self.client.put_object(Bucket=self.bucket, Key=config['prefix'] + '/' + ref.object_key,
                                       Body=b'corrupt', Metadata={'sha256': ref.sha256})

        def object_keys(self, backend):
            if backend == 'files':
                from pathlib import Path

                return {str(path) for path in Path(self.configs['files']['root']).rglob('*')
                        if path.is_file() and '.storage-identities' not in path.parts}
            return {row['Key'] for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket)
                    for row in page.get('Contents', []) if '/.storage-identities/' not in row['Key']}

        def observe(self, scenario, source, target, **details):
            fixtures.observations.setdefault('storage_object_transfers', []).append({
                'scenario': scenario, 'source_identity': asdict(source.identity),
                'target_identity': asdict(target.identity), **details})

        def test_files_and_s3_copy_both_directions_repeat_and_reopen_with_explicit_identity(self):
            for source_backend, target_backend in (('files', 's3'), ('s3', 'files')):
                direction = source_backend + '-to-' + target_backend
                with self.subTest(direction=direction):
                    _, source = self.open(source_backend, direction + '-source')
                    target_provider, target = self.open(target_backend, direction + '-target')
                    values = (('tenant-a', b'\x00\xff synthetic original'), ('tenant-a', b''),
                              ('tenant-b', '合成原文，迁移后逐字节保留。'.encode()))
                    records = []
                    for tenant, data in values:
                        ref = source.put(tenant, data)
                        records.append({'tenant_id': tenant, 'locator': ref.locator, 'sha256': ref.sha256})
                    result = self.transfer(source, target, records)
                    self.assertNotEqual(source.identity.dataset_id, target.identity.dataset_id)
                    self.assertEqual(len(result['records']), len(values))
                    self.assertEqual(self.transfer(source, target, records), result)
                    target_provider.close()
                    _, reopened = self.open(target_backend, direction + '-target', target.identity.dataset_id)
                    self.assertEqual(reopened.identity, target.identity)
                    for row, (tenant, data) in zip(result['records'], values, strict=True):
                        self.assertNotEqual(row['source_locator'], row['target_locator'])
                        self.assertEqual(source.read(tenant, row['source_locator'], sha256=row['sha256']), data)
                        self.assertEqual(reopened.read(tenant, row['target_locator'], sha256=row['sha256']), data)
                    self.observe('copy-repeat-reopen', source, reopened, source_backend=source_backend,
                                 target_backend=target_backend, records=result['records'],
                                 source_preserved=True, repeated_mapping_identical=True)

        def test_wrong_identity_foreign_namespace_and_corrupt_source_are_rejected_in_both_directions(self):
            for source_backend, target_backend in (('files', 's3'), ('s3', 'files')):
                direction = source_backend + '-to-' + target_backend
                with self.subTest(direction=direction):
                    _, source = self.open(source_backend, direction + '-source')
                    _, target = self.open(target_backend, direction + '-target')
                    ref = source.put('tenant', b'private original')
                    records = [{'tenant_id': 'tenant', 'locator': ref.locator, 'sha256': ref.sha256}]
                    before = self.object_keys(target_backend)
                    for changed in ({'source_id': 'wrong-source'}, {'target_id': 'wrong-target'}):
                        with self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
                            self.transfer(source, target, records, **changed)
                    _, other = self.open(source_backend, direction + '-foreign-project')
                    with self.assertRaises((ValueError, StorageError)):
                        self.transfer(other, target, records)
                    with self.assertRaises((ValueError, StorageError)):
                        self.transfer(source, target, [{**records[0], 'tenant_id': 'intruder'}])
                    with self.assertRaises((ValueError, StorageError)):
                        self.transfer(source, target, [{**records[0], 'sha256': '0' * 64}])
                    self.assertEqual(source.read('tenant', ref.locator), b'private original')
                    self.corrupt(source_backend, ref)
                    with self.assertRaises((ValueError, StorageError)):
                        self.transfer(source, target, records)
                    self.assertEqual(self.object_keys(target_backend), before)
                    self.observe('identity-namespace-tenant-hash-corruption-rejection', source, target,
                                 source_backend=source_backend, target_backend=target_backend,
                                 rejected_cases=6)

        def test_corrupt_target_is_not_overwritten_when_repeating_either_direction(self):
            for source_backend, target_backend in (('files', 's3'), ('s3', 'files')):
                direction = source_backend + '-to-' + target_backend
                with self.subTest(direction=direction):
                    _, source = self.open(source_backend, direction + '-source')
                    _, target = self.open(target_backend, direction + '-target')
                    ref = source.put('tenant', b'immutable source')
                    records = [{'tenant_id': 'tenant', 'locator': ref.locator, 'sha256': ref.sha256}]
                    result = self.transfer(source, target, records)
                    copied = target.reference('tenant', result['records'][0]['target_locator'])
                    self.corrupt(target_backend, copied)
                    with self.assertRaises((ValueError, StorageError)):
                        self.transfer(source, target, records)
                    with self.assertRaises((ValueError, StorageError)):
                        target.read('tenant', copied.locator)
                    self.assertEqual(source.read('tenant', ref.locator), b'immutable source')
                    self.observe('corrupt-target-retry-rejection', source, target,
                                 source_backend=source_backend, target_backend=target_backend,
                                 source_preserved=True, target_corruption_preserved=True)

    return unittest.defaultTestLoader.loadTestsFromTestCase(StorageObjectIntegration)

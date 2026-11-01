from dataclasses import asdict
import re

from .blob_service import verify_content
from .storage_sdk import ExistingDataset, OBJECT_V1, StorageError, StorageIdentity


MAX_TRANSFER_RECORDS = 1000
MAX_TRANSFER_BYTES = 256 * 1024 * 1024


def transfer_objects(source, target, records, *, expected_source_dataset_id, expected_target_dataset_id):
    for store, expected, side in ((source, expected_source_dataset_id, 'source'),
                                  (target, expected_target_dataset_id, 'target')):
        OBJECT_V1.validate(store)
        ExistingDataset(expected)
        if not isinstance(store.identity, StorageIdentity) or store.identity.dataset_id != expected:
            raise StorageError('STORAGE_DATASET_IDENTITY_MISMATCH', side=side)
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= MAX_TRANSFER_RECORDS:
        raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
    planned, total_bytes = [], 0
    for record in records:
        if (not isinstance(record, dict) or set(record) != {'tenant_id', 'locator', 'sha256'}
                or any(not isinstance(record[key], str) or not 1 <= len(record[key]) <= 1024
                       for key in ('tenant_id', 'locator'))
                or not isinstance(record['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise StorageError('STORAGE_OBJECT_TRANSFER_RECORD_INVALID')
        ref = source.reference(record['tenant_id'], record['locator'], sha256=record['sha256'])
        total_bytes += ref.size
        if ref.size > 64 * 1024 * 1024 or total_bytes > MAX_TRANSFER_BYTES:
            raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
        planned.append((record['tenant_id'], ref))
    mapping = []
    for tenant, ref in planned:
        data = source.read(tenant, ref.locator, sha256=ref.sha256)
        verify_content(ref, data)
        copied = target.put(tenant, data)
        if copied.sha256 != ref.sha256 or copied.size != ref.size:
            raise StorageError('STORAGE_OBJECT_TRANSFER_INTEGRITY_FAILURE')
        target.reference(tenant, copied.locator, sha256=ref.sha256)
        restored = target.read(tenant, copied.locator, sha256=ref.sha256)
        verify_content(copied, restored)
        if restored != data:
            raise StorageError('STORAGE_OBJECT_TRANSFER_INTEGRITY_FAILURE')
        mapping.append({'tenant_id': tenant, 'source_locator': ref.locator, 'target_locator': copied.locator,
                        'sha256': ref.sha256, 'byte_length': ref.size})
    return {'version': 1, 'source_identity': asdict(source.identity), 'target_identity': asdict(target.identity),
            'records': mapping}

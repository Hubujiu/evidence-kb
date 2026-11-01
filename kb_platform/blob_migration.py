from hashlib import sha256
import json
import os
from pathlib import Path


def _legacy_bytes(record, root, max_bytes):
    if (not isinstance(record, dict) or set(record) != {'tenant_id', 'original_path', 'original_sha256'}
            or any(not isinstance(value, str) or not value for value in record.values())):
        raise ValueError('INVALID_LEGACY_ORIGINAL_RECORD')
    checksum = record['original_sha256']
    if len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
        raise ValueError('INVALID_LEGACY_ORIGINAL_HASH')
    expected = root / sha256(record['tenant_id'].encode('utf-8')).hexdigest() / checksum[:2] / checksum
    actual = Path(record['original_path']).resolve(strict=True)
    if actual != expected or not actual.is_relative_to(root):
        raise ValueError('LEGACY_ORIGINAL_PATH_MISMATCH')
    with actual.open('rb') as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('LEGACY_ORIGINAL_SIZE_LIMIT')
    if sha256(data).hexdigest() != checksum:
        raise ValueError('LEGACY_ORIGINAL_INTEGRITY_FAILURE')
    return data


def migrate_legacy_originals(records, *, legacy_root, target, output, max_bytes=64 * 1024 * 1024):
    if not isinstance(records, list) or not records or type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('INVALID_LEGACY_ORIGINAL_PLAN')
    root, directory = Path(legacy_root).resolve(strict=True), Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    def write(name, value):
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    write('plan.json', {'version': 1, 'legacy_root': str(root), 'records': records,
        'scope': 'Explicit offline original migration; database references are not modified.'})
    completed = []
    try:
        for record in records:
            _legacy_bytes(record, root, max_bytes)
        with (directory / 'receipts.jsonl').open('x', encoding='utf-8') as receipts:
            for record in records:
                data = _legacy_bytes(record, root, max_bytes)
                locator, checksum = target.put(record['tenant_id'], data)
                if checksum != record['original_sha256'] or target.read(locator, record['tenant_id'], checksum) != data:
                    raise ValueError('MIGRATED_ORIGINAL_INTEGRITY_FAILURE')
                result = {**record, 'blob_locator': locator, 'byte_length': len(data)}
                receipts.write(json.dumps(result, ensure_ascii=False) + '\n')
                receipts.flush()
                os.fsync(receipts.fileno())
                completed.append(result)
        write('mapping.json', {'version': 1, 'records': completed})
        write('summary.json', {'status': 'COMPLETE', 'planned': len(records), 'completed': len(completed),
                               'database_updated': False, 'new_model_calls': 0})
        return completed
    except BaseException as error:
        write('summary.json', {'status': 'ERROR', 'planned': len(records), 'completed': len(completed),
                               'error_type': type(error).__name__, 'database_updated': False, 'new_model_calls': 0})
        raise

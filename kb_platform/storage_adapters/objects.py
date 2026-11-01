from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from ..blob_service import BlobRef, verify_content
from ..storage import FileBlobProvider, S3BlobProvider
from ..storage_sdk import ExistingDataset, OBJECT_V1, StorageError, StorageExport, StorageIdentity


class ObjectStorageProvider:
    def __init__(self, provider):
        self._provider = provider

    def exports(self):
        return (StorageExport('object', (OBJECT_V1,), self.open_space),)

    def _read_identity(self, space):
        if isinstance(self._provider, FileBlobProvider):
            path = self._provider.root / '.storage-identities' / (space.digest + '.json')
            if not path.exists():
                return None
            data = path.read_bytes()
        else:
            from botocore.exceptions import ClientError

            try:
                result = self._provider.client.get_object(Bucket=self._provider.bucket,
                    Key=self._provider.prefix + '/.storage-identities/' + space.digest + '.json')
            except ClientError as error:
                if self._provider._missing(error):
                    return None
                raise StorageError('STORAGE_OBJECT_IDENTITY_UNAVAILABLE') from None
            try:
                data = result['Body'].read(4097)
            finally:
                result['Body'].close()
        try:
            value = json.loads(data)
            if (len(data) > 4096 or set(value) != {'version', 'space', 'dataset_id'}
                    or value['version'] != 1 or value['space'] != space.key
                    or not isinstance(value['dataset_id'], str) or not value['dataset_id']):
                raise ValueError('identity')
            return StorageIdentity(space, value['dataset_id'])
        except (ValueError, TypeError):
            raise StorageError('STORAGE_OBJECT_IDENTITY_CORRUPT') from None

    def initialize(self, export_name, space):
        if export_name != 'object':
            raise ValueError('INVALID_STORAGE_INITIALIZATION')
        existing = self._read_identity(space)
        if existing is not None:
            return existing
        identity = StorageIdentity(space, uuid4().hex)
        data = json.dumps({'version': 1, 'space': space.key, 'dataset_id': identity.dataset_id},
                          sort_keys=True, separators=(',', ':')).encode('utf-8')
        if isinstance(self._provider, FileBlobProvider):
            import os

            path = self._provider.root / '.storage-identities' / (space.digest + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.parent / (uuid4().hex + '.tmp')
            try:
                with temporary.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        else:
            from botocore.exceptions import ClientError

            try:
                self._provider.client.put_object(Bucket=self._provider.bucket,
                    Key=self._provider.prefix + '/.storage-identities/' + space.digest + '.json',
                    Body=data, IfNoneMatch='*')
            except ClientError as error:
                if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') not in {409, 412}:
                    raise StorageError('STORAGE_OBJECT_IDENTITY_UNAVAILABLE') from None
        return self._read_identity(space)

    def open_space(self, space, target):
        if not isinstance(target, ExistingDataset):
            raise StorageError('STORAGE_PERSISTENT_DATASET_REQUIRED')
        identity = self._read_identity(space)
        if identity is None or identity.dataset_id != target.dataset_id:
            raise StorageError('STORAGE_DATASET_IDENTITY_MISMATCH')
        return ScopedObjectStore(self._provider, identity)

    def close(self):
        self._provider.close()


class ScopedObjectStore:
    def __init__(self, provider, identity):
        self._provider, self.identity = provider, identity
        self._namespace = sha256((identity.space.key + ':' + identity.dataset_id).encode('utf-8')).hexdigest()

    def reference(self, tenant_id, locator, *, sha256=None):
        ref = BlobRef.parse(locator)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError('INVALID_BLOB_IDENTITY')
        if ref.space != self._namespace or ref.tenant != _tenant(tenant_id):
            raise ValueError('BLOB_ACCESS_DENIED')
        if sha256 is not None and ref.sha256 != sha256:
            raise ValueError('BLOB_INTEGRITY_FAILURE')
        return ref

    def put(self, tenant_id, data):
        if not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
            raise ValueError('INVALID_BLOB_CONTENT')
        ref = BlobRef(self._namespace, _tenant(tenant_id), sha256(data).hexdigest(), len(data))
        self._provider.put_if_absent(ref, data)
        return ref

    def read(self, tenant_id, locator, *, sha256=None):
        ref = self.reference(tenant_id, locator, sha256=sha256)
        data = self._provider.read(ref)
        verify_content(ref, data)
        return data

    def stat(self, tenant_id, locator, *, sha256=None):
        ref = self.reference(tenant_id, locator, sha256=sha256)
        self._provider.stat(ref)
        return ref


def _tenant(tenant_id):
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError('INVALID_BLOB_IDENTITY')
    return sha256(tenant_id.encode('utf-8')).hexdigest()


def file_objects(config):
    if set(config) != {'root'} or not isinstance(config['root'], (str, Path)):
        raise ValueError('INVALID_FILE_STORAGE_CONFIGURATION')
    return ObjectStorageProvider(FileBlobProvider(config['root']))


def s3_objects(config):
    return ObjectStorageProvider(S3BlobProvider(**config))

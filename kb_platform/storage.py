from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from .blob_service import BlobRef, BlobService, verify_content


class FileBlobProvider:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.closed = False

    def path(self, ref: BlobRef) -> Path:
        if self.closed:
            raise ValueError('BLOB_PROVIDER_CLOSED')
        path = self.root.joinpath(*ref.object_key.split('/')).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('BLOB_SPACE_ESCAPE')
        return path

    def put_if_absent(self, ref: BlobRef, data: bytes) -> None:
        verify_content(ref, data)
        target = self.path(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f'.{uuid4().hex}.tmp'
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(ref)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, ref: BlobRef) -> bytes:
        with self.path(ref).open('rb') as stream:
            data = stream.read(ref.size + 1)
        verify_content(ref, data)
        return data

    def stat(self, ref: BlobRef) -> BlobRef:
        self.read(ref)
        return ref

    def close(self):
        self.closed = True


class S3BlobProvider:
    def __init__(self, *, endpoint_url, bucket, access_key, secret_key, region='us-east-1',
                 prefix='originals', max_bytes=64 * 1024 * 1024):
        from urllib.parse import urlsplit
        import boto3
        from botocore.config import Config

        endpoint = urlsplit(endpoint_url)
        if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {'', '/'}):
            raise ValueError('INVALID_BLOB_ENDPOINT')
        if (not isinstance(bucket, str) or not bucket or '/' in bucket or not isinstance(prefix, str)
                or any(part in {'.', '..', ''} for part in prefix.split('/'))
                or type(max_bytes) is not int or max_bytes < 1):
            raise ValueError('INVALID_BLOB_BUCKET')
        self.bucket, self.prefix, self.max_bytes = bucket, prefix, max_bytes
        self.closed = False
        self.client = boto3.client('s3', endpoint_url=endpoint_url, aws_access_key_id=access_key,
            aws_secret_access_key=secret_key, region_name=region,
            config=Config(retries={'total_max_attempts': 1, 'mode': 'standard'},
                          request_checksum_calculation='when_required', response_checksum_validation='when_required',
                          connect_timeout=5, read_timeout=20, s3={'addressing_style': 'path'}))
        self._disable_region_redirects()

    def _disable_region_redirects(self):
        from botocore.utils import S3RegionRedirectorv2

        events = self.client.meta.events
        handlers = list(events._emitter._handlers.prefix_search('needs-retry.s3'))
        for handler in handlers:
            owner = getattr(handler, '__self__', None)
            if isinstance(owner, S3RegionRedirectorv2):
                for event, method in (('needs-retry.s3', owner.redirect_from_error),
                        ('before-parameter-build.s3', owner.annotate_request_context),
                        ('before-endpoint-resolution.s3', owner.redirect_from_cache)):
                    events.unregister(event, method)

    def _key(self, ref):
        if self.closed:
            raise ValueError('BLOB_PROVIDER_CLOSED')
        if ref.size > self.max_bytes:
            raise ValueError('BLOB_SIZE_LIMIT')
        return self.prefix + '/' + ref.object_key

    @staticmethod
    def _missing(error):
        return error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404

    def put_if_absent(self, ref: BlobRef, data: bytes) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        verify_content(ref, data)
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(ref), Body=data,
                ContentLength=ref.size, IfNoneMatch='*',
                Metadata={'sha256': ref.sha256}, ContentType='application/octet-stream')
        except ClientError as error:
            status = error.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            if status not in {409, 412}:
                raise OSError('BLOB_WRITE_FAILED') from error
            verify_content(ref, self.read(ref))
        except BotoCoreError as error:
            raise OSError('BLOB_WRITE_FAILED') from error

    def read(self, ref: BlobRef) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self._key(ref))
        except ClientError as error:
            if self._missing(error):
                raise FileNotFoundError('BLOB_NOT_FOUND') from error
            raise OSError('BLOB_READ_FAILED') from error
        except BotoCoreError as error:
            raise OSError('BLOB_READ_FAILED') from error
        body = result['Body']
        try:
            if result.get('ContentLength') != ref.size or result.get('Metadata', {}).get('sha256') != ref.sha256:
                raise ValueError('BLOB_INTEGRITY_FAILURE')
            try:
                data = body.read(ref.size + 1)
            except BotoCoreError as error:
                raise OSError('BLOB_READ_FAILED') from error
            verify_content(ref, data)
            return data
        finally:
            body.close()

    def stat(self, ref: BlobRef) -> BlobRef:
        self.read(ref)
        return ref

    def close(self):
        if not self.closed:
            self.client.close()
            self.closed = True


class OriginalStore:
    def __init__(self, root=None, *, bound=None, space='platform.originals'):
        if (root is None) == (bound is None):
            raise ValueError('ORIGINAL_STORE_REQUIRES_ONE_BACKEND')
        self._owned = None
        if bound is None:
            self.root = Path(root).resolve()
            self._owned = BlobService()
            self._owned.register('files', FileBlobProvider(self.root))
            bound = self._owned.bind('files', space=space)
        self._bound = bound

    def put(self, tenant_id: str, data: bytes) -> tuple[str, str]:
        ref = self._bound.put(tenant_id, data)
        return ref.locator, ref.sha256

    def read(self, locator: str, tenant_id: str, digest: str) -> bytes:
        return self._bound.read(tenant_id, locator, sha256=digest)

    def stat(self, locator: str, tenant_id: str, digest: str) -> BlobRef:
        return self._bound.stat(tenant_id, locator, sha256=digest)

    def checked_path(self, locator: str, tenant_id: str, digest: str) -> Path:
        ref = self._bound.reference(tenant_id, locator, sha256=digest)
        self._bound.read(tenant_id, locator, sha256=digest)
        with self._bound.lease() as provider:
            if not isinstance(provider, FileBlobProvider):
                raise ValueError('BLOB_BACKEND_HAS_NO_LOCAL_PATH')
            return provider.path(ref)

    def close(self):
        if self._owned is not None:
            self._owned.close()

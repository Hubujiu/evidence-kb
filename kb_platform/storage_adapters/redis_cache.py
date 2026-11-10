from contextlib import contextmanager
from hashlib import sha256
import math
from threading import RLock
from urllib.parse import urlsplit

import redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from ..storage_sdk import (
    EphemeralCacheSpace, KV_EXPIRING_V1, KV_V1, SpaceKey, StorageError, StorageExport, StorageIdentity,
)
from .values import validate_key, validate_value


MAX_TTL_SECONDS = 365 * 24 * 60 * 60


class RedisCacheProvider:
    def __init__(self, url, *, timeout_seconds=5):
        if (not isinstance(url, str) or not url.startswith(('redis://', 'rediss://')) or
                type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or
                timeout_seconds <= 0 or timeout_seconds > 60):
            raise StorageError('STORAGE_INVALID_REDIS_CONFIG')
        try:
            parsed = urlsplit(url)
            if not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError('INVALID_REDIS_URL')
            self._client = redis.Redis.from_url(url, socket_connect_timeout=timeout_seconds,
                                                socket_timeout=timeout_seconds, decode_responses=False,
                                                retry_on_timeout=False, retry=Retry(NoBackoff(), 0))
        except (ValueError, RedisError):
            raise StorageError('STORAGE_INVALID_REDIS_CONFIG') from None
        self._closed = False
        self._closing = False
        self._active = 0
        self._lock = RLock()

    def exports(self):
        return (StorageExport('kv', (KV_V1, KV_EXPIRING_V1), self.open_space),)

    def initialize(self, export_name, space):
        raise StorageError('STORAGE_CACHE_DATASET_UNSUPPORTED')

    def open_space(self, space, target):
        if not isinstance(space, SpaceKey) or not isinstance(target, EphemeralCacheSpace):
            raise StorageError('STORAGE_CACHE_TARGET_REQUIRED')
        with self._connection() as client:
            client.ping()
        return RedisCache(self, space, target.cache_id)

    @contextmanager
    def _connection(self):
        with self._lock:
            if self._closing:
                raise StorageError('STORAGE_PROVIDER_CLOSED')
            self._active += 1
            try:
                yield self._client
            except RedisError:
                raise StorageError('STORAGE_BACKEND_UNAVAILABLE') from None
            finally:
                self._active -= 1

    def close(self):
        with self._lock:
            self._closing = True
            if self._active:
                raise StorageError('STORAGE_PROVIDER_BUSY')
            if not self._closed:
                self._active += 1
                try:
                    self._client.close()
                    self._closed = True
                finally:
                    self._active -= 1


class RedisCache:
    def __init__(self, provider, space, cache_id):
        self._provider = provider
        namespace = sha256((space.key + '\x00' + cache_id).encode('utf-8')).hexdigest()
        self._prefix = 'kb:cache:v1:{' + namespace + '}:'
        self.identity = StorageIdentity(space, cache_id)

    def _key(self, key):
        return self._prefix + sha256(validate_key(key).encode('utf-8')).hexdigest()

    def get(self, key):
        redis_key = self._key(key)
        with self._provider._connection() as client:
            value = client.get(redis_key)
        return None if value is None else validate_value(value)

    def set(self, key, value):
        redis_key, payload = self._key(key), validate_value(value)
        with self._provider._connection() as client:
            client.set(redis_key, payload)

    def put_expiring(self, key, value, *, ttl_seconds):
        redis_key, payload = self._key(key), validate_value(value)
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or
                ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS):
            raise StorageError('STORAGE_INVALID_TTL')
        milliseconds = max(1, math.ceil(ttl_seconds * 1000))
        with self._provider._connection() as client:
            client.set(redis_key, payload, px=milliseconds)

    def delete(self, key):
        redis_key = self._key(key)
        with self._provider._connection() as client:
            return client.delete(redis_key) == 1

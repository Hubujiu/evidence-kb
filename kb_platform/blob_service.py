from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
import re
from threading import RLock
from uuid import uuid4


def _hash(value):
    if not isinstance(value, str) or not value:
        raise ValueError('INVALID_BLOB_IDENTITY')
    return sha256(value.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class BlobRef:
    space: str
    tenant: str
    sha256: str
    size: int

    def __post_init__(self):
        if (any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
                for value in (self.space, self.tenant, self.sha256))
                or type(self.size) is not int or self.size < 0):
            raise ValueError('INVALID_BLOB_REFERENCE')

    @property
    def object_key(self):
        return '/'.join((self.space, self.tenant, self.sha256[:2], self.sha256))

    @property
    def locator(self):
        return f'blob:v1:{self.space}:{self.tenant}:{self.sha256}:{self.size}'

    @classmethod
    def parse(cls, locator):
        if not isinstance(locator, str):
            raise ValueError('INVALID_BLOB_REFERENCE')
        parts = locator.split(':')
        if len(parts) != 6 or parts[:2] != ['blob', 'v1'] or not re.fullmatch('0|[1-9][0-9]*', parts[5]):
            raise ValueError('INVALID_BLOB_REFERENCE')
        return cls(*parts[2:5], int(parts[5]))


def verify_content(ref, data):
    if not isinstance(data, bytes) or len(data) != ref.size or sha256(data).hexdigest() != ref.sha256:
        raise ValueError('BLOB_INTEGRITY_FAILURE')


@dataclass(eq=False)
class _Route:
    provider: object
    registration: object
    owner_lease: object
    active: int = 0
    phase: str = 'active'


class BlobService:
    def __init__(self):
        self._routes = {}
        self._registrations = set()
        self._lock = RLock()
        self._state = 'active'

    def register(self, route, provider, *, registration_id=None, owner_lease=None):
        if (not isinstance(route, str) or not route or any(not callable(getattr(provider, name, None))
                for name in ('put_if_absent', 'read', 'stat', 'close'))):
            raise ValueError('INVALID_BLOB_PROVIDER')
        identity = uuid4().hex if registration_id is None else registration_id
        if not isinstance(identity, (str, int)) or isinstance(identity, bool) or not identity:
            raise ValueError('INVALID_BLOB_REGISTRATION')
        with self._lock:
            if self._state != 'active' or route in self._routes or identity in self._registrations:
                raise ValueError('BLOB_REGISTRATION_UNAVAILABLE')
            entry = _Route(provider, identity, owner_lease)
            self._routes[route] = entry
            self._registrations.add(identity)
        return lambda: self._dispose(route, entry)

    def _entry(self, route, expected=None):
        entry = self._routes.get(route)
        if self._state != 'active' or entry is None or entry.phase != 'active':
            raise ValueError('BLOB_ROUTE_UNAVAILABLE')
        if expected is not None and entry.registration != expected:
            raise ValueError('BLOB_BINDING_CHANGED')
        return entry

    def bind(self, route, *, space, expected=None):
        with self._lock:
            entry = self._entry(route, expected)
            return BoundBlobStore(self, route, entry.registration, _hash(space))

    @contextmanager
    def lease(self, route, registration):
        with self._lock:
            entry = self._entry(route, registration)
            owner_lease = entry.owner_lease
        with owner_lease() if owner_lease is not None else nullcontext():
            with self._lock:
                entry = self._entry(route, registration)
                entry.active += 1
            try:
                yield entry.provider
            finally:
                with self._lock:
                    entry.active -= 1

    def _dispose(self, route, entry):
        with self._lock:
            if self._routes.get(route) is not entry:
                return
            if entry.phase == 'cleaning':
                raise ValueError('BLOB_CLEANUP_BUSY')
            entry.phase = 'draining'
            if entry.active:
                raise ValueError('BLOB_BUSY')
            entry.phase = 'cleaning'
        try:
            entry.provider.close()
        except BaseException:
            with self._lock:
                entry.phase = 'cleanup_failed'
            raise
        with self._lock:
            del self._routes[route]

    def inspect(self):
        with self._lock:
            return {route: {'registration_id': row.registration, 'phase': row.phase,
                            'active_calls': row.active} for route, row in self._routes.items()}

    def close(self):
        with self._lock:
            if any(row.active for row in self._routes.values()):
                raise ValueError('BLOB_BUSY')
            self._state = 'draining'
            rows = list(reversed(list(self._routes.items())))
        for route, entry in rows:
            self._dispose(route, entry)
        with self._lock:
            self._state = 'closed'


class BoundBlobStore:
    def __init__(self, service, route, registration, space):
        self._service, self._route, self.registration_id, self.space = service, route, registration, space

    def lease(self):
        return self._service.lease(self._route, self.registration_id)

    def reference(self, tenant_id, locator, *, sha256=None):
        ref = BlobRef.parse(locator)
        if ref.space != self.space or ref.tenant != _hash(tenant_id):
            raise ValueError('BLOB_ACCESS_DENIED')
        if sha256 is not None and ref.sha256 != sha256:
            raise ValueError('BLOB_INTEGRITY_FAILURE')
        return ref

    def put(self, tenant_id, data):
        if not isinstance(data, bytes):
            raise ValueError('INVALID_BLOB_CONTENT')
        ref = BlobRef(self.space, _hash(tenant_id), sha256(data).hexdigest(), len(data))
        with self.lease() as provider:
            provider.put_if_absent(ref, data)
        return ref

    def read(self, tenant_id, locator, *, sha256=None):
        ref = self.reference(tenant_id, locator, sha256=sha256)
        with self.lease() as provider:
            data = provider.read(ref)
            verify_content(ref, data)
            return data

    def stat(self, tenant_id, locator, *, sha256=None):
        ref = self.reference(tenant_id, locator, sha256=sha256)
        with self.lease() as provider:
            actual = provider.stat(ref)
            if actual != ref:
                raise ValueError('BLOB_INTEGRITY_FAILURE')
            return actual

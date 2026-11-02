from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from .database_contracts import DatabaseError, QueryLimits, SchemaPackage, SpaceKey, SqlStatement
from .database_migrations import initialize_space, prepare


@dataclass(frozen=True)
class DatabaseGrant:
    route: str
    space: SpaceKey
    consumer_identity: object
    registration_id: str
    token: str
    expected_dataset_id: str | None = None


@dataclass
class _Route:
    adapter: object
    registration_id: str
    owner_identity: object
    owner_lease: object
    owned: bool
    phase: str = 'active'
    active: int = 0


class DatabaseService:
    def __init__(self):
        self._routes, self._grants, self._issued = {}, {}, set()
        self._lock, self._state = RLock(), 'active'

    def register(self, name, adapter, *, owner_identity=None, owner_lease=None,
                 registration_id=None, owned=True):
        if not isinstance(name, str) or not name or not callable(getattr(adapter, 'open_space', None)):
            raise ValueError('INVALID_DATABASE_REGISTRATION')
        registration = str(registration_id) if registration_id is not None else uuid4().hex
        identity = _identity(owner_identity)
        registration_key = (identity, registration)
        route = _Route(adapter, registration, identity, owner_lease, owned)
        with self._lock:
            if self._state != 'active' or name in self._routes:
                raise DatabaseError('DATABASE_ROUTE_UNAVAILABLE')
            if registration_key in self._issued:
                raise DatabaseError('DATABASE_REGISTRATION_REUSED')
            self._routes[name] = route
            self._issued.add(registration_key)
        return lambda: self._dispose(name, route)

    def _entry(self, name, registration=None):
        route = self._routes.get(name)
        if self._state != 'active' or route is None or route.phase != 'active':
            raise DatabaseError('DATABASE_ROUTE_UNAVAILABLE')
        if registration is not None and route.registration_id != registration:
            raise DatabaseError('DATABASE_BINDING_CHANGED')
        return route

    def grant(self, route, space, consumer_identity, *, expected_dataset_id=None):
        if not isinstance(space, SpaceKey) or consumer_identity is None:
            raise ValueError('INVALID_DATABASE_GRANT')
        with self._lock:
            entry = self._entry(route)
            grant = DatabaseGrant(route, space, _identity(consumer_identity), entry.registration_id,
                                  uuid4().hex, expected_dataset_id)
            self._grants[grant.token] = grant
            return grant

    def bind(self, grant, *, owner_lease=None):
        with self._lock:
            if not isinstance(grant, DatabaseGrant) or self._grants.get(grant.token) is not grant:
                raise DatabaseError('DATABASE_GRANT_REJECTED')
        with self.lease(grant.route, grant.registration_id) as adapter:
            space = adapter.open_space(grant.space)
            initialize_space(space, expected_dataset_id=grant.expected_dataset_id)
        return BoundDatabase(self, grant, space, owner_lease=owner_lease)

    @contextmanager
    def lease(self, name, registration):
        with self._lock:
            route = self._entry(name, registration)
            ownership = route.owner_lease() if route.owner_lease is not None else nullcontext()
        with ownership:
            with self._lock:
                route = self._entry(name, registration)
                route.active += 1
            try:
                yield route.adapter
            finally:
                with self._lock:
                    route.active -= 1

    def catalog(self):
        with self._lock:
            return {name: row.adapter.info for name, row in self._routes.items() if row.phase == 'active'}

    def inspect(self):
        with self._lock:
            return {name: {'registration_id': row.registration_id, 'owner_identity': row.owner_identity,
                           'phase': row.phase, 'active_calls': row.active} for name, row in self._routes.items()}

    def _dispose(self, name, route):
        with self._lock:
            if self._routes.get(name) is not route:
                return
            if route.phase == 'closing':
                raise DatabaseError('DATABASE_CLEANUP_BUSY')
            route.phase = 'draining'
            if route.active:
                raise DatabaseError('DATABASE_ROUTE_BUSY')
            route.phase = 'closing'
        try:
            if route.owned:
                route.adapter.close()
        except BaseException:
            with self._lock:
                route.phase = 'cleanup_failed'
            raise
        with self._lock:
            del self._routes[name]
            for token, grant in tuple(self._grants.items()):
                if grant.route == name and grant.registration_id == route.registration_id:
                    del self._grants[token]

    def close(self):
        with self._lock:
            if any(row.active for row in self._routes.values()):
                raise DatabaseError('DATABASE_ROUTE_BUSY')
            self._state = 'draining'
            rows = tuple(self._routes.items())
        for name, route in reversed(rows):
            self._dispose(name, route)
        with self._lock:
            self._state = 'closed'


class BoundDatabase:
    def __init__(self, service, grant, space, *, owner_lease=None, consumer_lease=None):
        self._service, self._grant, self._space = service, grant, space
        self._owner_lease, self._consumer_lease = owner_lease, consumer_lease
        self._sealed = False
        self.identity, self.info = space.identity, space.info
        self.physical_name = self.schema = space.physical_name
        self.runtime_role = space.runtime_engine.url.username

    @property
    def registration_id(self):
        return self._grant.registration_id

    def for_consumer(self, identity, lease, *, consumer_name=None):
        if consumer_name is not None and self._grant.consumer_identity != consumer_name:
            raise DatabaseError('DATABASE_CONSUMER_GRANT_MISMATCH')
        bound = BoundDatabase(self._service, self._grant, self._space,
                              owner_lease=self._owner_lease, consumer_lease=lease)
        bound._consumer_identity = _identity(identity)
        bound._sealed = self._sealed
        return bound

    @contextmanager
    def _lease(self, *, include_consumer=True):
        ownership = self._owner_lease() if self._owner_lease is not None else nullcontext()
        consumer = self._consumer_lease() if include_consumer and self._consumer_lease is not None else nullcontext()
        with consumer, ownership, self._service.lease(self._grant.route, self._grant.registration_id):
            yield

    def prepare(self, package):
        if self._sealed or not isinstance(package, SchemaPackage):
            raise DatabaseError('DATABASE_PREPARE_CLOSED')
        with self._lease(include_consumer=False):
            return prepare(self._space, package)

    def seal(self):
        self._sealed = True
        return self

    @contextmanager
    def transaction(self, *, write=True, mode=None, limits=None, operation_id=None):
        if mode is not None:
            if mode not in {'read', 'write'}:
                raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
            write = mode == 'write'
        if type(write) is not bool:
            raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
        self._sealed = True
        with self._lease(), self._space.transaction(write=write, limits=limits, operation_id=operation_id) as tx:
            yield tx

    def batch(self, statements, *, limits=None):
        statements = tuple(statements)
        selected = QueryLimits() if limits is None else limits
        if not statements or len(statements) > selected.max_statements:
            raise DatabaseError('DATABASE_BATCH_LIMIT')
        if any(not isinstance(statement, SqlStatement) for statement in statements):
            raise ValueError('INVALID_DATABASE_BATCH')
        with self.transaction(write=True, limits=selected) as tx:
            return tuple(tx.execute(statement) for statement in statements)

    def query_json(self, request):
        from .database_process import query_json

        return query_json(self, request)

    def batch_json(self, request):
        from .database_process import batch_json

        return batch_json(self, request)


def _identity(value):
    return tuple(sorted(dict(value).items())) if hasattr(value, 'items') else value

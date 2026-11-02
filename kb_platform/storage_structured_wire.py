from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from dataclasses import asdict
from queue import Empty, Queue
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from .database_contracts import CommitUnknown, DatabaseError, QueryLimits, QueryResult
from .database_process import encode_value
from .storage_expression_wire import (
    Decoder, decoded_value, expression, schema_descriptor, schema_semantics, statement,
)
from .storage_sdk.relational import StructuredSchema
from .storage_protocol import resolve_commit, validate_identity, validate_structured
from .storage_json_wire import (
    MAX_MESSAGE_BYTES as MAX_MESSAGE_BYTES, StorageJsonClient, StorageJsonEndpoint,
    wire as _wire,
)


MAX_HANDLES = 64
GUARD_LEASE_SECONDS = 60


def _limits(value):
    return QueryLimits(**value)


def _result(value):
    return {'rows': [{str(key): encode_value(item) for key, item in row.items()} for row in value.rows],
            'rowcount': value.rowcount}


class _Rollback(Exception):
    pass


class _Handle:
    def __init__(self, manager, *, kind, seconds, operation_id, finished):
        self.id, self.kind, self.operation_id = uuid4().hex, kind, operation_id
        self._manager, self._seconds, self._finished = manager, seconds, finished
        self._queue, self._ready, self._stopped = Queue(maxsize=64), Future(), Event()
        self._state_lock = RLock()
        self._thread = Thread(target=self._run, name='storage-' + self.id[:8], daemon=True)

    def start(self):
        self._thread.start()
        try:
            self._ready.result(timeout=self._seconds + 1)
        except TimeoutError:
            self._stopped.set()
            raise DatabaseError('STORAGE_HANDLE_TIMEOUT') from None

    def _run(self):
        manager, active, pending, completed, failure = None, False, None, None, None
        outcome = {'status': 'UNKNOWN', 'operation_id': self.operation_id}
        try:
            manager = self._manager()
            resource = manager.__enter__()
            active = True
            self._ready.set_result(None)
            deadline = monotonic() + self._seconds
            while not self._stopped.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise DatabaseError('STORAGE_HANDLE_EXPIRED')
                try:
                    action, pending = self._queue.get(timeout=remaining)
                except Empty:
                    raise DatabaseError('STORAGE_HANDLE_EXPIRED') from None
                if not pending.set_running_or_notify_cancel():
                    pending = None
                    continue
                if action in ('commit', 'rollback'):
                    active = False
                    if action == 'commit':
                        manager.__exit__(None, None, None)
                        outcome['status'] = 'COMMITTED' if self.kind == 'transaction' else 'RELEASED'
                    else:
                        error = _Rollback()
                        manager.__exit__(_Rollback, error, None)
                        outcome['status'] = 'ROLLED_BACK'
                    completed, pending = pending, None
                    break
                if action == 'ping':
                    if self.kind != 'guard':
                        raise DatabaseError('STORAGE_HANDLE_KIND_INVALID')
                    result = {'status': 'HELD'}
                    deadline = monotonic() + self._seconds
                else:
                    result = _wire(action(resource))
                pending.set_result(result)
                pending = None
        except BaseException as error:
            if active:
                active = False
                try:
                    manager.__exit__(type(error), error, error.__traceback__)
                    if not isinstance(error, CommitUnknown):
                        outcome['status'] = 'ROLLED_BACK'
                except BaseException as cleanup_error:
                    error = cleanup_error
            if not self._ready.done():
                self._ready.set_exception(error)
            if pending is not None and not pending.done():
                completed, failure = pending, error
        finally:
            if active:
                try:
                    error = _Rollback()
                    manager.__exit__(_Rollback, error, None)
                    outcome['status'] = 'ROLLED_BACK'
                except BaseException:
                    outcome['status'] = 'UNKNOWN'
            with self._state_lock:
                self._stopped.set()
                while True:
                    try:
                        _, queued = self._queue.get_nowait()
                        if not queued.done():
                            queued.set_exception(DatabaseError('STORAGE_HANDLE_CLOSED'))
                    except Empty:
                        break
            self._finished(self.id, outcome)
            if completed is not None:
                completed.set_exception(failure) if failure is not None else completed.set_result(outcome)

    def call(self, action):
        with self._state_lock:
            if self._stopped.is_set():
                raise DatabaseError('STORAGE_HANDLE_CLOSED')
            pending = Future()
            try:
                self._queue.put_nowait((action, pending))
            except Exception:
                raise DatabaseError('STORAGE_HANDLE_BUSY') from None
        try:
            return pending.result(timeout=self._seconds + 1)
        except TimeoutError:
            pending.cancel()
            self._stopped.set()
            if action == 'commit' and self.kind == 'transaction':
                raise CommitUnknown(self.operation_id) from None
            raise DatabaseError('STORAGE_HANDLE_TIMEOUT') from None

    def close(self):
        if not self._stopped.is_set():
            try:
                self.call('rollback')
            except DatabaseError as error:
                if error.code != 'STORAGE_HANDLE_CLOSED':
                    raise
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise DatabaseError('STORAGE_HANDLE_BUSY')


class PortableStructuredEndpoint:
    def __init__(self, store, *, schemas):
        self._store, self._schemas = store, tuple(schemas)
        if not self._schemas or any(not isinstance(item, StructuredSchema) for item in self._schemas):
            raise ValueError('STORAGE_WIRE_TRUSTED_SCHEMA_REQUIRED')
        validate_identity(asdict(store.identity))
        self._lock, self._handles, self._outcomes = RLock(), {}, {}
        self._package, self._sealed, self._closed = None, False, False
        self._transport = StorageJsonEndpoint(self._execute, self._validate_transfer_transaction)

    def _finished(self, token, outcome):
        with self._lock:
            self._handles.pop(token, None)
            self._outcomes[token] = outcome
            while len(self._outcomes) > 256:
                self._outcomes.pop(next(iter(self._outcomes)))
        self._transport.discard_transaction(token)

    def _validate_transfer_transaction(self, token):
        with self._lock:
            handle = self._handles.get(token) if type(token) is str else None
            if self._closed or handle is None or handle.kind != 'transaction' or handle._stopped.is_set():
                raise DatabaseError('STORAGE_HANDLE_CLOSED')

    def _open(self, manager, *, kind, seconds, operation_id=None):
        with self._lock:
            if self._closed:
                raise DatabaseError('STORAGE_WIRE_CLOSED')
            if len(self._handles) >= MAX_HANDLES:
                raise DatabaseError('STORAGE_HANDLE_LIMIT')
            handle = _Handle(manager, kind=kind, seconds=seconds, operation_id=operation_id, finished=self._finished)
            self._handles[handle.id] = handle
        handle.start()
        return {'handle': handle.id, 'operation_id': operation_id}

    def execute(self, request):
        return self._transport.execute(request)

    def _execute(self, request):
        validate_structured(request)
        with self._lock:
            if self._closed:
                raise DatabaseError('STORAGE_WIRE_CLOSED')
            operation = request.get('op')
            if operation == 'schemas':
                return _wire({'schemas': [schema_descriptor(item) for item in self._schemas]})
            if operation == 'prepare':
                selected = next((item for item in self._schemas if item.owner == request['owner']
                                 and item.version == request['schema_version'] and item.digest == request['digest']), None)
                if selected is None:
                    raise DatabaseError('STORAGE_WIRE_SCHEMA_UNDECLARED')
                if self._package is not None:
                    if self._package.digest != selected.digest:
                        raise DatabaseError('STORAGE_STRUCTURED_SCHEMA_MISMATCH')
                    return {'status': 'PREPARED'}
                if self._sealed:
                    raise DatabaseError('DATABASE_PREPARE_CLOSED')
                self._store.prepare(selected)
                self._package = selected
                return {'status': 'PREPARED'}
            if self._package is None:
                raise DatabaseError('STORAGE_STRUCTURED_SCHEMA_REQUIRED')
            if operation == 'seal':
                self._store.seal()
                self._sealed = True
                return {'status': 'SEALED'}
        if operation == 'begin':
            limits = _limits(request['limits'])
            scope, write, operation_id = request['scope'], request['write'], request['operation_id']
            return self._open(lambda: self._store.transaction(write=write, scope=scope, limits=limits,
                              operation_id=operation_id), kind='transaction', seconds=limits.max_seconds,
                              operation_id=operation_id)
        if operation == 'guard':
            return self._open(lambda: self._store.guard(request['name'], nowait=request['nowait']),
                              kind='guard', seconds=GUARD_LEASE_SECONDS)
        token = request['handle']
        with self._lock:
            handle, outcome = self._handles.get(token), self._outcomes.get(token)
        if operation == 'finish':
            if handle is None and outcome is not None:
                if outcome['status'] == 'UNKNOWN':
                    if outcome['operation_id'] is None:
                        raise DatabaseError('STORAGE_GUARD_RELEASE_UNKNOWN')
                    raise CommitUnknown(outcome['operation_id'])
                return _wire(outcome)
        if handle is None:
            raise DatabaseError('STORAGE_HANDLE_CLOSED')
        if operation == 'finish':
            return _wire(handle.call('commit' if request['commit'] else 'rollback'))
        if operation == 'ping':
            return handle.call('ping')
        if handle.kind != 'transaction':
            raise DatabaseError('STORAGE_HANDLE_KIND_INVALID')
        if operation not in ('execute', 'insert'):
            raise DatabaseError('STORAGE_WIRE_OPERATION_UNSUPPORTED')

        def run(tx):
            decoder = Decoder(self._package.metadata)
            if operation == 'execute':
                result = _result(tx.execute(decoder.statement(request['statement'])))
                return self._transport.result(result, transaction=token)
            table = decoder.table(request['table'])
            keys = request['conflict_keys']
            if any(key not in table.c for key in keys):
                raise DatabaseError('INVALID_STRUCTURED_CONFLICT_KEYS')
            updates = None if request['updates'] is None else decoder.values(table, request['updates'])
            returning = tuple(decoder.expression(item) for item in decoder.items(request['returning']))
            result = _result(tx.insert(table, decoder.values(table, request['values']), conflict_keys=keys,
                                      updates=updates, returning=returning))
            return self._transport.result(result, transaction=token)

        try:
            return handle.call(run)
        except SQLAlchemyError:
            raise DatabaseError('STORAGE_WIRE_OPERATION_FAILED') from None

    def close(self):
        with self._lock:
            self._closed = True
            handles = tuple(self._handles.values())
        self._transport.close()
        failures = []
        for handle in handles:
            try:
                handle.close()
            except BaseException as error:
                failures.append(error)
        if failures:
            raise failures[0]


class _Transaction:
    def __init__(self, client, token):
        self._client, self._token = client, token

    def _result(self, request):
        result = self._client._call({**request, 'handle': self._token})
        return QueryResult(({key: decoded_value(value) for key, value in row.items()} for row in result['rows']),
                           result['rowcount'])

    def execute(self, query, params=None):
        return self._result({'op': 'execute', 'statement': statement(query, params)})

    def insert(self, table, values, *, conflict_keys=(), updates=None, returning=()):
        return self._result({'op': 'insert', 'table': str(table.name),
                            'values': {str(key): expression(value) for key, value in values.items()},
                            'conflict_keys': list(conflict_keys),
                            'updates': None if updates is None else {str(key): expression(value) for key, value in updates.items()},
                            'returning': [expression(value) for value in returning]})


class PortableStructuredClient:
    def __init__(self, call, *, identity):
        self._send, self.identity = call, identity
        validate_identity(asdict(identity))
        self._transport = StorageJsonClient(lambda request: self._send(request))

    def _call(self, request):
        return self._transport.call({'version': 1, **request})

    def prepare(self, package):
        if not isinstance(package, StructuredSchema):
            raise DatabaseError('STORAGE_WIRE_TRUSTED_SCHEMA_REQUIRED')
        if package.owner != self.identity.space.owner_id:
            raise DatabaseError('STORAGE_WIRE_SCHEMA_UNDECLARED')
        expected = schema_semantics(schema_descriptor(package, include_digest=False))
        advertised = self._call({'op': 'schemas'})
        if type(advertised) is not dict or set(advertised) != {'schemas'} or type(advertised['schemas']) is not list:
            raise DatabaseError('STORAGE_WIRE_SCHEMA_INVALID')
        matches = [item for item in advertised['schemas'] if schema_semantics(item) == expected]
        if (len(matches) != 1 or type(matches[0].get('digest')) is not str or not matches[0]['digest']):
            raise DatabaseError('STORAGE_WIRE_SCHEMA_UNDECLARED')
        self._call({'op': 'prepare', 'owner': package.owner, 'schema_version': package.version,
                    'digest': matches[0]['digest']})
        return self

    def seal(self):
        self._call({'op': 'seal'})
        return self

    @contextmanager
    def transaction(self, *, write=True, scope=None, limits=None, operation_id=None):
        opened = self._call({'op': 'begin', 'write': write, 'scope': {} if scope is None else dict(scope),
                             'limits': asdict(QueryLimits() if limits is None else limits),
                             'operation_id': uuid4().hex if operation_id is None else operation_id})
        token = opened['handle']
        try:
            yield _Transaction(self, token)
        except BaseException:
            try:
                self._call({'op': 'finish', 'handle': token, 'commit': False})
            except BaseException:
                pass
            raise
        else:
            try:
                result = self._call({'op': 'finish', 'handle': token, 'commit': True})
            except Exception as error:
                try:
                    resolve_commit(opened['operation_id'], None)
                except CommitUnknown as unknown:
                    raise unknown from error
            resolve_commit(opened['operation_id'], result)

    @contextmanager
    def guard(self, name, *, nowait=True):
        opened = self._call({'op': 'guard', 'name': name, 'nowait': nowait})
        token, stopped, failures = opened['handle'], Event(), []

        def heartbeat():
            while not stopped.wait(GUARD_LEASE_SECONDS / 3):
                try:
                    self._call({'op': 'ping', 'handle': token})
                except BaseException as error:
                    failures.append(error)
                    return

        worker = Thread(target=heartbeat, name='storage-guard-heartbeat', daemon=True)
        worker.start()
        failure = None
        try:
            yield
            if failures:
                raise failures[0]
        except BaseException as error:
            failure = error
            raise
        finally:
            stopped.set()
            worker.join(timeout=1)
            try:
                result = self._call({'op': 'finish', 'handle': token, 'commit': True})
                if result['status'] != 'RELEASED':
                    raise DatabaseError('STORAGE_GUARD_LOST')
            except BaseException:
                if failure is None:
                    raise

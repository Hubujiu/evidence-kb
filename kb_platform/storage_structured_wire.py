from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from dataclasses import asdict
import json
from queue import Empty, Queue
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from .database_contracts import CommitUnknown, DatabaseError, QueryLimits, QueryResult
from .database_process import encode_value
from .storage_expression_wire import (
    Decoder, decoded_value, expression, fields, schema_descriptor, schema_semantics, statement,
)
from .storage_sdk.relational import StructuredSchema


MAX_HANDLES = 64
MAX_MESSAGE_BYTES = 4 * 1024 * 1024 - 4096
MAX_STATEMENTS = 20000
GUARD_LEASE_SECONDS = 60


def _wire(value):
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 48 or nodes > 1000000:
            raise DatabaseError('STORAGE_WIRE_STRUCTURE_LIMIT')
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED')
            for child in item.values():
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED')

    visit(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_MESSAGE_BYTES:
            raise DatabaseError('STORAGE_WIRE_MESSAGE_LIMIT')
        return json.loads(encoded)
    except (ValueError, TypeError, UnicodeError):
        raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED') from None


def _limits(value):
    fields(value, {'max_rows', 'max_bytes', 'max_statements', 'max_seconds'}, ())
    try:
        limits = QueryLimits(**value)
    except (ValueError, TypeError):
        raise DatabaseError('INVALID_DATABASE_LIMITS') from None
    if (limits.max_rows > 10000 or limits.max_bytes > 8 * 1024 * 1024
            or limits.max_statements > MAX_STATEMENTS):
        raise DatabaseError('DATABASE_QUERY_LIMIT')
    return limits


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
        outcome = {'status': 'ROLLED_BACK', 'operation_id': self.operation_id}
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
            if isinstance(error, CommitUnknown):
                outcome['status'] = 'UNKNOWN'
            if active:
                active = False
                try:
                    manager.__exit__(type(error), error, error.__traceback__)
                except BaseException as cleanup_error:
                    error = cleanup_error
                    if isinstance(cleanup_error, CommitUnknown):
                        outcome['status'] = 'UNKNOWN'
            if not self._ready.done():
                self._ready.set_exception(error)
            if pending is not None and not pending.done():
                completed, failure = pending, error
        finally:
            if active:
                try:
                    error = _Rollback()
                    manager.__exit__(_Rollback, error, None)
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
        self._lock, self._handles, self._outcomes = RLock(), {}, {}
        self._package, self._sealed, self._closed = None, False, False

    def _finished(self, token, outcome):
        with self._lock:
            self._handles.pop(token, None)
            self._outcomes[token] = outcome
            while len(self._outcomes) > 256:
                self._outcomes.pop(next(iter(self._outcomes)))

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
        request = _wire(request)
        if type(request) is not dict or request.get('version') != 1 or type(request.get('version')) is not int:
            raise DatabaseError('STORAGE_WIRE_VERSION_INVALID')
        with self._lock:
            if self._closed:
                raise DatabaseError('STORAGE_WIRE_CLOSED')
            operation = request.get('op')
            if operation == 'schemas':
                fields(request, {'version', 'op'})
                return _wire({'schemas': [schema_descriptor(item) for item in self._schemas]})
            if operation == 'prepare':
                fields(request, {'version', 'op', 'owner', 'schema_version', 'digest'})
                if (type(request['owner']) is not str or type(request['schema_version']) is not int
                        or type(request['digest']) is not str):
                    raise DatabaseError('STORAGE_WIRE_SCHEMA_UNDECLARED')
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
                fields(request, {'version', 'op'})
                self._store.seal()
                self._sealed = True
                return {'status': 'SEALED'}
        if operation == 'begin':
            fields(request, {'version', 'op', 'write', 'scope', 'limits', 'operation_id'})
            limits = _limits(request['limits'])
            scope, write, operation_id = request['scope'], request['write'], request['operation_id']
            if (type(write) is not bool or type(scope) is not dict or type(operation_id) is not str
                    or not operation_id or len(operation_id) > 200):
                raise DatabaseError('STORAGE_WIRE_TRANSACTION_INVALID')
            return self._open(lambda: self._store.transaction(write=write, scope=scope, limits=limits,
                              operation_id=operation_id), kind='transaction', seconds=limits.max_seconds,
                              operation_id=operation_id)
        if operation == 'guard':
            fields(request, {'version', 'op', 'name', 'nowait'})
            if type(request['name']) is not str or type(request['nowait']) is not bool:
                raise DatabaseError('INVALID_STORAGE_GUARD')
            return self._open(lambda: self._store.guard(request['name'], nowait=request['nowait']),
                              kind='guard', seconds=GUARD_LEASE_SECONDS)
        fields(request, {'version', 'op', 'handle', 'statement', 'table', 'values', 'conflict_keys', 'updates',
                         'returning', 'commit'}, {'version', 'op', 'handle'})
        token = request['handle']
        if type(token) is not str:
            raise DatabaseError('STORAGE_HANDLE_INVALID')
        with self._lock:
            handle, outcome = self._handles.get(token), self._outcomes.get(token)
        if operation == 'finish':
            fields(request, {'version', 'op', 'handle', 'commit'})
            if type(request['commit']) is not bool:
                raise DatabaseError('STORAGE_WIRE_TRANSACTION_INVALID')
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
            fields(request, {'version', 'op', 'handle'})
            return handle.call('ping')
        if handle.kind != 'transaction':
            raise DatabaseError('STORAGE_HANDLE_KIND_INVALID')
        if operation not in ('execute', 'insert'):
            raise DatabaseError('STORAGE_WIRE_OPERATION_UNSUPPORTED')

        def run(tx):
            decoder = Decoder(self._package.metadata)
            if operation == 'execute':
                fields(request, {'version', 'op', 'handle', 'statement'})
                return _result(tx.execute(decoder.statement(request['statement'])))
            fields(request, {'version', 'op', 'handle', 'table', 'values', 'conflict_keys', 'updates', 'returning'})
            table = decoder.table(request['table'])
            keys = request['conflict_keys']
            if type(keys) is not list or any(type(key) is not str or key not in table.c for key in keys):
                raise DatabaseError('INVALID_STRUCTURED_CONFLICT_KEYS')
            updates = None if request['updates'] is None else decoder.values(table, request['updates'])
            returning = tuple(decoder.expression(item) for item in decoder.items(request['returning']))
            return _result(tx.insert(table, decoder.values(table, request['values']), conflict_keys=keys,
                                     updates=updates, returning=returning))

        try:
            return handle.call(run)
        except SQLAlchemyError:
            raise DatabaseError('STORAGE_WIRE_OPERATION_FAILED') from None

    def close(self):
        with self._lock:
            self._closed = True
            handles = tuple(self._handles.values())
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

    def _call(self, request):
        return _wire(self._send(_wire({'version': 1, **request})))

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
                if getattr(error, 'code', None) in {
                        'PROCESS_RESULT_UNKNOWN', 'PROCESS_EXITED_UNKNOWN', 'CONTRACT_RESPONSE_INVALID',
                        'STORAGE_WIRE_JSON_REQUIRED', 'STORAGE_WIRE_MESSAGE_LIMIT', 'STORAGE_WIRE_STRUCTURE_LIMIT'}:
                    raise CommitUnknown(opened['operation_id']) from error
                raise
            if (type(result) is not dict or result.get('operation_id') != opened['operation_id']
                    or result.get('status') not in ('COMMITTED', 'ROLLED_BACK')):
                raise CommitUnknown(opened['operation_id'])
            if result['status'] == 'ROLLED_BACK':
                raise DatabaseError('STORAGE_TRANSACTION_ROLLED_BACK')

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

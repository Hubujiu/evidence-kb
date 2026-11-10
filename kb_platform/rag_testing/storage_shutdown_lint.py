import ast


PROVIDER = 'kb_platform/storage_adapters/redis_cache.py'
APPLICATION = 'kb_platform/application_storage.py'


def storage_shutdown_violations(provider_source, application_source):
    errors = []
    def method(source, owner, name):
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
        return next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)

    try:
        close = method(provider_source, 'RedisCacheProvider', 'close')
        expected = ast.parse('''
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
''').body[0]
        if ast.dump(close, include_attributes=False) != ast.dump(expected, include_attributes=False):
            errors.append('Redis close must seal admission, reject active/reentrant cleanup and acknowledge only successful physical close')
        connection = method(provider_source, 'RedisCacheProvider', '_connection')
        expected = ast.parse('''
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
''').body[0]
        if ast.dump(connection, include_attributes=False) != ast.dump(expected, include_attributes=False):
            errors.append('Redis connection must fence closing and balance active depth under its lock')
        application = method(application_source, 'ApplicationStorage', 'close')
        expected = ast.parse('''
def close(self):
    with self._lock:
        self._closing = True
        if self._active:
            raise StorageError('STORAGE_SERVICE_BUSY')
        for endpoints in (self._generic, self._structured, self._objects):
            for name, endpoint in reversed(tuple(endpoints.items())):
                endpoint.close()
                del endpoints[name]
        self._registry.close()
        while self._instances:
            self._instances[-1].close()
            self._instances.pop()
        self._bound.clear()
''').body[0]
        if ast.dump(application, include_attributes=False) != ast.dump(expected, include_attributes=False):
            errors.append('Application close must drain operations and retain each cleanup owner until successful return')
        operation = method(application_source, 'ApplicationStorage', '_operation')
        expected = ast.parse('''
@contextmanager
def _operation(self):
    with self._lock:
        if self._closing:
            raise StorageError('STORAGE_SERVICE_UNAVAILABLE')
        self._active += 1
    try:
        yield
    finally:
        with self._lock:
            self._active -= 1
''').body[0]
        if ast.dump(operation, include_attributes=False) != ast.dump(expected, include_attributes=False):
            errors.append('Application operations must reject closing and release active ownership in finally')
    except (SyntaxError, StopIteration):
        errors.append('Storage shutdown owners are missing or invalid')
    return errors

import ast
from pathlib import Path


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _has(node, source):
    expected = _dump(ast.parse(source).body[0])
    return any(_dump(child) == expected for child in ast.walk(node))


def sqlite_initialization_violations(source):
    errors = []
    expected = ast.parse('''
def _set_journal_mode(connection, mode):
    deadline = time.monotonic() + 30
    def read(statement):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SQLITE_JOURNAL_MODE_TIMEOUT')
        with closing(connection.execute('PRAGMA busy_timeout=' + str(int(remaining * 1000)))):
            pass
        with closing(connection.execute(statement)) as cursor:
            return cursor.fetchone()[0]
    while True:
        try:
            if read('PRAGMA journal_mode') != mode:
                if read('PRAGMA journal_mode=' + mode) != mode:
                    raise RuntimeError('SQLITE_WAL_UNAVAILABLE')
            return
        except sqlite3.OperationalError as error:
            code = getattr(error, 'sqlite_errorcode', None)
            if not isinstance(code, int) or code & 0xff != sqlite3.SQLITE_BUSY:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))
def _initialize(connection, record, *, wal):
    _set_journal_mode(connection, 'wal' if wal else 'delete')
    for statement in ('PRAGMA busy_timeout=30000', 'PRAGMA synchronous=FULL',
                      'PRAGMA foreign_keys=ON', 'PRAGMA recursive_triggers=ON'):
        with closing(connection.execute(statement)):
            pass
''').body
    try:
        tree = ast.parse(source)
        for function in expected:
            initializers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function.name]
            if len(initializers) != 1 or _dump(initializers[0]) != _dump(function):
                errors.append('sqlite-bootstrap: journal admission requires bounded BUSY-only retry with remaining-budget waits and cursor release')
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SQLiteProvider')
        opener = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == 'open_space')
        if not _has(opener, "event.listen(engine, 'connect', partial(_initialize, wal=self._files is None))"):
            errors.append('sqlite-bootstrap: both engines must use journal admission before handing out a connection')
        if not _has(opener, "pooling = {'poolclass': QueuePool, 'pool_size': 1, 'max_overflow': -1} if self._files is None else {'poolclass': NullPool}"):
            errors.append('sqlite-bootstrap: explicit legacy files must keep NullPool and DELETE without new sidecars')
    except (SyntaxError, StopIteration):
        errors.append('sqlite-bootstrap: initialization owner shape is invalid')
    return errors


def discovered_lifecycle_violations(root: Path):
    errors = []
    sqlite = root / 'kb_platform/database_providers/sqlite.py'
    if sqlite.is_file():
        errors.extend(sqlite_initialization_violations(sqlite.read_text('utf-8')))
    sources = {
        'memory': ('kb_platform/harness_sessions.py', 'MemorySession'),
        'raw_sql': ('kb_platform/storage_adapters/sql.py', '_SqlSpace'),
    }
    for owner, (path, name) in sources.items():
        file = root / path
        if not file.is_file():
            continue
        try:
            tree = ast.parse(file.read_text('utf-8'))
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
            methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
            if owner == 'memory':
                required = {
                    'append': ["if self._closed:\n    raise RuntimeError('SESSION_CLOSED')",
                               'self._records.extend(deepcopy(records))'],
                    'messages': ['return deepcopy(self._records)'],
                    'finish': ['self._closed = True', 'self._records.clear()'],
                    'fail': ['self._closed = True', 'self._records.clear()'],
                }
                for method, statements in required.items():
                    if [_dump(n) for n in methods[method].body] != [
                            _dump(ast.parse(item).body[0]) for item in statements]:
                        errors.append(f'discovered-lifecycle: memory {method} must retain reviewed close/copy ordering')
            else:
                prepare = "if self._sealed or not isinstance(package, SchemaPackage):\n    raise DatabaseError('DATABASE_PREPARE_CLOSED')"
                if not _has(methods['prepare'], prepare):
                    errors.append('discovered-lifecycle: raw SQL prepare requires unsealed valid package')
                required = ast.parse("""if mode is not None:
    if mode not in {'read', 'write'}:
        raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
    write = mode == 'write'
if type(write) is not bool:
    raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
self._sealed = True
with self._space.transaction(write=write, limits=limits, operation_id=operation_id) as tx:
    yield tx
""").body
                if [_dump(n) for n in methods['transaction'].body] != [_dump(n) for n in required]:
                    errors.append('discovered-lifecycle: raw SQL validates then seals before physical transaction entry')
        except (SyntaxError, StopIteration, KeyError):
            errors.append(f'discovered-lifecycle: missing reviewed {owner} owner')
    file = root / 'scripts/delivery_control.py'
    if file.is_file():
        try:
            tree = ast.parse(file.read_text('utf-8'))
            functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            start, stop = functions['start'], functions['stop']
            launch = next(n for n in ast.walk(start) if isinstance(n, ast.Try)
                          and any(_has(child, 'pidfd = os.pidfd_open(process.pid)') for child in n.body))
            statements = [_dump(n) for n in launch.body]
            required = ['pidfd = os.pidfd_open(process.pid)', 'write_receipt(receipt_path, receipt)',
                        'published = True', "os.write(write_fd, b'1')"]
            positions = [statements.index(_dump(ast.parse(item).body[0])) for item in required]
            if positions != sorted(positions):
                errors.append('discovered-lifecycle: delivery must acquire handle and publish receipt before gate release')
            check = "if actual_start != receipt['process_start'] or executable != Path(receipt['host_binary']).resolve(strict=True):\n    raise ValueError('Recorded PID belongs to a different process.')"
            marker = "if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink != 1:\n    raise ValueError('Existing stop marker is not a private regular file.') from None"
            if not _has(stop, check) or not _has(stop, marker):
                errors.append('discovered-lifecycle: delivery stop requires exact process and private marker identity')
            stop_try = next(n for n in stop.body if isinstance(n, ast.Try)
                            and any(_has(child, 'actual_start = process_start(pid)') for child in n.body))
            ordered = [_dump(n) for n in stop_try.body]
            identity_index = ordered.index(_dump(ast.parse(check).body[0]))
            create_index = next(i for i, n in enumerate(stop_try.body)
                                if _has(n, 'fd = os.open(marker, flags, 0o600)'))
            if identity_index >= create_index:
                errors.append('discovered-lifecycle: process identity must precede stop marker effects')
            containment = "if receipt_path.parent != run or marker.parent.resolve(strict=True) != run or marker.name != 'stop.request':\n    raise ValueError('Stop marker must belong to the recorded server run.')"
            if not _has(stop, containment):
                errors.append('discovered-lifecycle: stop marker must remain in recorded run directory')
            rollback = launch.handlers[0].body[0]
            if not (isinstance(rollback, ast.If) and _dump(rollback.test) == _dump(ast.parse('published', mode='eval').body)
                    and len(rollback.body) == 1 and isinstance(rollback.body[0], ast.Raise)):
                errors.append('discovered-lifecycle: published launch failure must remain unknown rather than rollback')
            if not _has(stop, 'flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW'):
                errors.append('discovered-lifecycle: delivery marker requires exclusive no-follow creation')
            checks = [n for n in ast.walk(start) if isinstance(n, ast.If)
                      and 'X-Delivery-Instance' in ast.unparse(n.test)]
            expected = "response.status == 200 and response.headers.get('X-Delivery-Profile') == profile and response.headers.get('X-Delivery-Instance') == instance"
            if len(checks) != 1 or _dump(checks[0].test) != _dump(ast.parse(expected, mode='eval').body):
                errors.append('discovered-lifecycle: delivery liveness requires exact profile and instance')
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {'kill', 'terminate', 'pidfd_send_signal'} for n in ast.walk(stop)):
                errors.append('discovered-lifecycle: stop timeout cannot force process termination')
        except (SyntaxError, StopIteration, KeyError, ValueError, IndexError):
            errors.append('discovered-lifecycle: delivery launch/stop ownership shape changed')
    return errors

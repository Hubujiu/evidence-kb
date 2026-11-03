import ast
from pathlib import Path


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _has(node, source):
    expected = _dump(ast.parse(source).body[0])
    return any(_dump(child) == expected for child in ast.walk(node))


def discovered_lifecycle_violations(root: Path):
    errors = []
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

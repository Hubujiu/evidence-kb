import ast
from pathlib import Path
import textwrap


def _tree(text):
    return ast.dump(ast.parse(textwrap.dedent(text)), include_attributes=False)


def violations(root: Path):
    errors = []
    for filename, cls, bound, entry, field, error, prefix, payload in (
        ('storage_service.py', 'StorageService', 'BoundStorage', 'entry', 'service', 'StorageError', 'STORAGE', 'export'),
        ('database_service.py', 'DatabaseService', 'BoundDatabase', 'route', 'route', 'DatabaseError', 'DATABASE', 'adapter'),
    ):
        path = root / 'kb_platform' / filename
        try:
            source = path.read_text('utf-8')
            classes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}
            methods = {node.name: node for node in classes[cls].body if isinstance(node, ast.FunctionDef)}
            expected = f'''
@contextmanager
def lease(self, name, registration, *, grant=None):
    with self._lock:
        {entry} = self._entry(name, registration)
        self._validate_lease_grant(grant, name, registration)
        ownership = {entry}.owner_lease() if {entry}.owner_lease is not None else nullcontext()
    with ownership:
        with self._lock:
            if self._entry(name, registration) is not {entry}:
                raise {error}('{prefix}_BINDING_CHANGED')
            self._validate_lease_grant(grant, name, registration)
            {entry}.active += 1
        try:
            yield {entry}.{payload}
        finally:
            with self._lock:
                {entry}.active -= 1
'''
            if _tree(ast.unparse(methods['lease'])) != _tree(expected):
                errors.append(f'{filename}: lease must fence captured entry and grant before active ownership')
            expected = f'''
def _validate_lease_grant(self, grant, name, registration):
    if grant is not None and (self._grants.get(grant.token) is not grant
            or grant.{field} != name or grant.registration_id != registration):
        raise {error}('{prefix}_GRANT_REJECTED')
'''
            if _tree(ast.unparse(methods['_validate_lease_grant'])) != _tree(expected):
                errors.append(f'{filename}: grant validation must preserve opaque identity and route binding')
            expected_bind_test = ast.parse(
                f'not isinstance(grant, {"StorageGrant" if prefix == "STORAGE" else "DatabaseGrant"}) or '
                'self._grants.get(grant.token) is not grant', mode='eval').body
            bind_guards = [node for node in ast.walk(methods['bind']) if isinstance(node, ast.If)]
            if not bind_guards or ast.dump(bind_guards[0].test) != ast.dump(expected_bind_test):
                errors.append(f'{filename}: bind must reject non-identical grant before entering owner code')
            bound_methods = {node.name: node for node in classes[bound].body if isinstance(node, ast.FunctionDef)}
            for method, owner, value in ((methods['bind'], 'self', 'grant'),
                                         (bound_methods['_lease'], 'self._service', 'self._grant')):
                calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)
                         and ast.unparse(node.func) == owner + '.lease']
                if len(calls) != 1 or [ast.unparse(k.value) for k in calls[0].keywords if k.arg == 'grant'] != [value]:
                    errors.append(f'{filename}:{method.name}: bound admission must forward its original grant')
        except (OSError, SyntaxError, KeyError, ValueError) as error:
            errors.append(f'{filename}: invalid lease-review source ({type(error).__name__})')
    return errors

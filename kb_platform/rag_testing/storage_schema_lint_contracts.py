import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .formal_state_machines import source_digest


def schema_admission_violations(source):
    tree = ast.parse(source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    endpoint = {node.name: node for node in classes['PortableStructuredEndpoint'].body
                if isinstance(node, ast.FunctionDef)}
    client = {node.name: node for node in classes['PortableStructuredClient'].body
              if isinstance(node, ast.FunctionDef)}
    errors = []
    initial = [ast.unparse(node) for node in endpoint['__init__'].body]
    if 'self._prepared_schema = None' not in initial:
        errors.append('successful schema identity must start absent')
    prepare = next((node for node in ast.walk(endpoint['_execute']) if isinstance(node, ast.If)
                    and ast.unparse(node.test) == "operation == 'prepare'"), None)
    if prepare is None:
        return errors + ['prepare admission branch missing']
    statements = [ast.unparse(node) for node in prepare.body]
    if not statements or statements[0] != "requested = (request['owner'], request['schema_version'], request['digest'])":
        errors.append('prepared identity must include the requested owner, version and opaque digest')
    cached = next((node for node in prepare.body if isinstance(node, ast.If)
                   and ast.unparse(node.test) == 'self._package is not None'), None)
    expected_cached = """if self._package is not None:
    if self._prepared_schema != requested:
        raise DatabaseError('STORAGE_STRUCTURED_SCHEMA_MISMATCH')
    return {'status': 'PREPARED'}"""
    if cached is None or ast.unparse(cached) != expected_cached:
        errors.append('idempotent prepare must compare the successful immutable identity before acknowledging')
    expected_tail = ["if self._sealed:\n    raise DatabaseError('DATABASE_PREPARE_CLOSED')",
                     'self._store.prepare(selected)', 'self._package = selected',
                     'self._prepared_schema = requested', "return {'status': 'PREPARED'}"]
    if statements[-5:] != expected_tail:
        errors.append('publish readiness and identity only after backend prepare returns successfully')
    assignments = [node for node in ast.walk(endpoint['_execute']) if isinstance(node, ast.Assign)
                   and any(ast.unparse(target) == 'self._prepared_schema' for target in node.targets)]
    if len(assignments) != 1:
        errors.append('successful identity must not be rewritten by retries or failure handling')
    for operation, status in [('prepare', 'PREPARED'), ('seal', 'SEALED')]:
        nodes = client[operation].body
        expected = f"if response != {{'status': '{status}'}}:\n    raise DatabaseError('STORAGE_WIRE_SCHEMA_INVALID')"
        if len(nodes) < 3 or ast.unparse(nodes[-2]) != expected or ast.unparse(nodes[-1]) != 'return self':
            errors.append(f'{operation} must reject a malformed or absent acknowledgement before success')
        response = nodes[-3] if len(nodes) >= 3 else None
        if (not isinstance(response, ast.Assign) or [ast.unparse(target) for target in response.targets] != ['response']
                or not isinstance(response.value, ast.Call) or ast.unparse(response.value.func) != 'self._call'):
            errors.append(f'{operation} must check the actual call response')
    return errors


class StorageSchemaLintContracts(unittest.TestCase):
    def source(self):
        return (Path(__file__).parents[1] / 'storage_structured_wire.py').read_text('utf-8')

    def test_successful_schema_identity_and_acknowledgements_are_guarded(self):
        self.assertEqual(schema_admission_violations(self.source()), [])

    def test_refreshed_hash_cannot_hide_schema_identity_or_ack_regression(self):
        source = self.source()
        changes = [
            ('self._prepared_schema = None', "self._prepared_schema = 'premature'"),
            ("requested = (request['owner'], request['schema_version'], request['digest'])",
             "requested = (request['digest'],)"),
            ('if self._prepared_schema != requested:', 'if self._package.digest != selected.digest:'),
            ('if self._prepared_schema != requested:', 'if False:'),
            ('                self._store.prepare(selected)\n                self._package = selected',
             '                self._package = selected\n                self._store.prepare(selected)'),
            ('                self._store.prepare(selected)',
             '                self._prepared_schema = requested\n                self._store.prepare(selected)'),
            ('                self._prepared_schema = requested\n', ''),
            ("if response != {'status': 'PREPARED'}:", 'if False:'),
            ("if response != {'status': 'SEALED'}:", 'if False:'),
            ("response = self._call({'op': 'seal'})", "self._call({'op': 'seal'})\n        response = {'status': 'SEALED'}"),
        ]
        binding = {'path': 'kb_platform/storage_structured_wire.py', 'language': 'python', 'symbol': '<module>'}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / binding['path']
            path.parent.mkdir()
            path.write_text(source, encoding='utf-8')
            original_hash = source_digest(root, binding)
            for old, new in changes:
                with self.subTest(old=old):
                    self.assertIn(old, source)
                    mutant = source.replace(old, new, 1)
                    path.write_text(mutant, encoding='utf-8')
                    refreshed = {**binding, 'sha256': source_digest(root, binding)}
                    self.assertNotEqual(refreshed['sha256'], original_hash)
                    self.assertEqual(refreshed['sha256'], source_digest(root, refreshed))
                    self.assertTrue(schema_admission_violations(mutant))

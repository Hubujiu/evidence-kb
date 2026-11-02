import ast
from pathlib import Path
import unittest


def handle_ownership_violations(source):
    tree = ast.parse(source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    handle = {node.name: node for node in classes['_Handle'].body if isinstance(node, ast.FunctionDef)}
    client = {node.name: node for node in classes['PortableStructuredClient'].body if isinstance(node, ast.FunctionDef)}
    errors = []

    def statements(nodes):
        return [ast.unparse(node) for node in nodes]

    start = handle['start']
    locked = start.body[0]
    if not isinstance(locked, ast.With) or ast.unparse(locked.items[0].context_expr) != 'self._state_lock':
        errors.append('launch must serialize admission with close')
    else:
        expected_guards = [
            "if self._stopped.is_set():\n    raise DatabaseError('STORAGE_HANDLE_CLOSED')",
            "if self._launched:\n    raise DatabaseError('STORAGE_HANDLE_BUSY')",
        ]
        if statements(locked.body[:2]) != expected_guards:
            errors.append('stopped and duplicate launches must be rejected before native start')
        launch = locked.body[2] if len(locked.body) > 2 else None
        if (not isinstance(launch, ast.Try) or statements(launch.body) != ['self._thread.start()']
                or len(launch.handlers) != 1 or ast.unparse(launch.handlers[0].type) != 'Exception'
                or statements(launch.handlers[0].body) != [
                    'self._stopped.set()',
                    "self._finished(self.id, {'status': 'UNKNOWN', 'operation_id': self.operation_id})", 'raise']):
            errors.append('known failed native launch must retire once as UNKNOWN and preserve the original exception')
        if statements(locked.body[3:]) != ['self._launched = True']:
            errors.append('mark launched only after successful native start')
    timeout = start.body[1] if len(start.body) > 1 else None
    if (not isinstance(timeout, ast.Try) or len(timeout.handlers) != 1
            or statements(timeout.handlers[0].body) != [
                'self._stopped.set()', "raise DatabaseError('STORAGE_HANDLE_TIMEOUT') from None"]):
        errors.append('ready timeout must retain a launched worker until real terminal cleanup')
    initial = statements(handle['__init__'].body)
    if 'self._launched = False' not in initial:
        errors.append('handles must initially be unlaunched')
    close = handle['close'].body[0]
    expected = """with self._state_lock:
    if not self._launched:
        if not self._stopped.is_set():
            self._stopped.set()
            self._finished(self.id, {'status': 'UNKNOWN', 'operation_id': self.operation_id})
        return"""
    if ast.unparse(close) != expected:
        errors.append('unstarted close must retire once and return before call or join')
    drains = [node for node in ast.walk(handle['_run']) if isinstance(node, ast.If)
              and any(isinstance(item, ast.Call) and ast.unparse(item.func) == 'queued.set_exception'
                      for item in ast.walk(node))]
    if (len(drains) != 1 or ast.unparse(drains[0].test) != 'queued.set_running_or_notify_cancel()'
            or statements(drains[0].body) != ["queued.set_exception(DatabaseError('STORAGE_HANDLE_CLOSED'))"]):
        errors.append('drain must atomically claim the queued Future before publishing close')
    guard = client['guard']
    protected = [node for node in guard.body if isinstance(node, ast.Try)]
    if len(protected) != 1 or statements(protected[0].body[:3]) != ['worker.start()', 'launched = True', 'yield']:
        errors.append('heartbeat startup must be covered by guard cleanup')
    else:
        final = protected[0].finalbody
        if statements(final[:2]) != ['stopped.set()', 'if launched:\n    worker.join(timeout=1)']:
            errors.append('heartbeat cleanup must not join an unlaunched worker')
        if statements(protected[0].handlers[0].body) != ['failure = error', 'raise']:
            errors.append('heartbeat cleanup must preserve the primary failure')
    return errors


class StorageHandleLintContracts(unittest.TestCase):
    def source(self):
        return (Path(__file__).parents[1] / 'storage_structured_wire.py').read_text('utf-8')

    def test_handle_ownership_structure_is_mandatory(self):
        self.assertEqual(handle_ownership_violations(self.source()), [])

    def test_seeded_launch_drain_and_heartbeat_regressions_are_rejected(self):
        source = self.source()
        changes = [
            ('with self._state_lock:\n            if self._stopped.is_set():',
             'with self._state_lock:\n            if False:'),
            ('if self._launched:\n                raise', 'if False:\n                raise'),
            ('self._launched = False', 'self._launched = True'),
            ('except Exception:\n                self._stopped.set()',
             'except BaseException:\n                self._stopped.set()'),
            ("self._finished(self.id, {'status': 'UNKNOWN', 'operation_id': self.operation_id})",
             "self._finished(self.id, {'status': 'ROLLED_BACK', 'operation_id': self.operation_id})"),
            ('                raise\n            self._launched = True',
             "                raise RuntimeError('masked')\n            self._launched = True"),
            ('        except TimeoutError:\n            self._stopped.set()',
             "        except TimeoutError:\n            self._finished(self.id, {'status': 'UNKNOWN'})\n            self._stopped.set()"),
            ('if not self._launched:', 'if False:'),
            ('                return\n        if not self._stopped.is_set():',
             '                self._thread.join()\n        if not self._stopped.is_set():'),
            ('if queued.set_running_or_notify_cancel():', 'if not queued.done():'),
            ('        try:\n            worker.start()\n            launched = True',
             '        worker.start()\n        try:\n            launched = True'),
            ('            if launched:\n                worker.join(timeout=1)',
             '            worker.join(timeout=1)'),
        ]
        for old, new in changes:
            with self.subTest(old=old):
                mutant = source.replace(old, new, 1)
                self.assertNotEqual(mutant, source)
                self.assertTrue(handle_ownership_violations(mutant))

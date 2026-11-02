import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from kb_platform.database_contracts import DatabaseError
from kb_platform.database_service import DatabaseService


def registry_cleanup_violations(source):
    tree = ast.parse(source)
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DatabaseService')
    methods = {node.name: node for node in service.body if isinstance(node, ast.FunctionDef)}
    errors = []
    locked = methods['_dispose'].body[0].body
    guards = [index for index, node in enumerate(locked) if isinstance(node, ast.If)
              and ast.unparse(node.test) == "route.phase == 'closing'"
              and any(isinstance(item, ast.Raise) for item in node.body)]
    draining = [index for index, node in enumerate(locked) if isinstance(node, ast.Assign)
                and ast.unparse(node) == "route.phase = 'draining'"]
    if len(guards) != 1 or len(draining) != 1 or guards[0] >= draining[0]:
        errors.append('reject concurrent cleanup before changing the route phase')
    locked = methods['close'].body[0].body
    draining = [index for index, node in enumerate(locked) if isinstance(node, ast.Assign)
                and ast.unparse(node) == "self._state = 'draining'"]
    snapshots = [index for index, node in enumerate(locked) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'rows' for target in node.targets)]
    if len(draining) != 1 or len(snapshots) != 1 or draining[0] >= snapshots[0]:
        errors.append('close admission under the lock before taking the disposal snapshot')
    return errors


class DatabaseRegistryLifecycleContracts(unittest.TestCase):
    @staticmethod
    def adapter(close):
        return SimpleNamespace(open_space=Mock(), close=close)

    def test_concurrent_disposers_never_close_the_same_adapter_twice(self):
        entered, release, calls = Event(), Event(), []

        def cleanup():
            calls.append('close')
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('synthetic cleanup wait timed out')

        service = DatabaseService()
        dispose = service.register('records', self.adapter(cleanup))
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(dispose)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_CLEANUP_BUSY'):
                    dispose()
                self.assertEqual(service.inspect()['records']['phase'], 'closing')
                self.assertEqual(calls, ['close'])
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(service.inspect(), {})

    def test_close_blocks_registration_and_competing_close_before_cleanup_finishes(self):
        entered, release, calls = Event(), Event(), []

        def cleanup():
            calls.append('close')
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('synthetic cleanup wait timed out')

        service = DatabaseService()
        service.register('records', self.adapter(cleanup))
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(service.close)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_ROUTE_UNAVAILABLE'):
                    service.register('late', self.adapter(Mock()))
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_CLEANUP_BUSY'):
                    service.close()
                self.assertEqual(calls, ['close'])
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(service.inspect(), {})
        self.assertEqual(service._state, 'closed')

    def test_failed_cleanup_retains_owner_for_explicit_retry_and_keeps_admission_closed(self):
        cleanup = Mock(side_effect=[RuntimeError('synthetic close failure'), None])
        service = DatabaseService()
        service.register('records', self.adapter(cleanup))
        with self.assertRaisesRegex(RuntimeError, 'synthetic close failure'):
            service.close()
        self.assertEqual(service.inspect()['records']['phase'], 'cleanup_failed')
        with self.assertRaisesRegex(DatabaseError, 'DATABASE_ROUTE_UNAVAILABLE'):
            service.register('late', self.adapter(Mock()))
        service.close()
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(service.inspect(), {})
        self.assertEqual(service._state, 'closed')

    def test_active_lease_is_not_released_by_dispose_or_close(self):
        cleanup = Mock()
        service = DatabaseService()
        dispose = service.register('records', self.adapter(cleanup), registration_id='generation-1')
        with service.lease('records', 'generation-1'):
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_ROUTE_BUSY'):
                dispose()
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_ROUTE_BUSY'):
                service.close()
            cleanup.assert_not_called()
        dispose()
        cleanup.assert_called_once()
        self.assertEqual(service.inspect(), {})

    def test_source_cleanup_guards_and_seeded_mutants(self):
        source = (Path(__file__).parents[1] / 'database_service.py').read_text('utf-8')
        self.assertEqual(registry_cleanup_violations(source), [])
        mutants = [source.replace("if route.phase == 'closing':", 'if False:', 1),
                   source.replace("            self._state = 'draining'\n", '', 1)]
        for mutant in mutants:
            self.assertNotEqual(mutant, source)
            self.assertTrue(registry_cleanup_violations(mutant))

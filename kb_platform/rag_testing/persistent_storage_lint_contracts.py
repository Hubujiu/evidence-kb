import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .formal_state_machines import source_digest


def migration_cleanup_violations(source):
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'migration_connection')
    outer = next((node for node in function.body if isinstance(node, ast.Try)), None)
    if outer is None or len(outer.finalbody) != 1 or not isinstance(outer.finalbody[0], ast.Try):
        return ['migration cleanup must protect close with an independent finally']
    cleanup = outer.finalbody[0]
    errors = []
    expected_initial = 'failure = None'
    if expected_initial not in [ast.unparse(node) for node in function.body]:
        errors.append('primary failure must start absent')
    expected_rollback = """except BaseException as error:
    failure = error
    try:
        connection.rollback()
    except BaseException as rollback_error:
        failure.add_note('Migration rollback failed: ' + type(rollback_error).__name__)
    raise"""
    if [ast.unparse(node) for node in outer.handlers] != [expected_rollback]:
        errors.append('rollback failure must not replace the primary migration error')
    expected_close = """try:
    connection.close()
except BaseException as close_error:
    if failure is None:
        raise
    failure.add_note('Migration close failed: ' + type(close_error).__name__)"""
    if [ast.unparse(node) for node in cleanup.finalbody] != [expected_close]:
        errors.append('always close the owned connection without replacing the primary error')
    expected_release_error = """except BaseException as release_error:
    if failure is None:
        failure = release_error
    else:
        failure.add_note('Migration lock release failed: ' + type(release_error).__name__)
    try:
        connection.invalidate()
    except BaseException as invalidate_error:
        failure.add_note('Migration invalidation failed: ' + type(invalidate_error).__name__)
    if failure is release_error:
        raise"""
    if [ast.unparse(node) for node in cleanup.handlers] != [expected_release_error] or cleanup.orelse:
        errors.append('release error must discard its session and preserve the earliest failure')
    expected_release = """if mysql_lock:
    connection.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': 'migration_' + space.key.digest[:48]})
    connection.commit()"""
    if len(cleanup.body) != 1 or ast.unparse(cleanup.body[0]) != expected_release:
        errors.append('only the acquired mysql lock is released before connection close')
    return errors


class PersistentStorageLintContracts(unittest.TestCase):
    def test_migration_cleanup_is_unconditional_and_preserves_primary_error(self):
        source = (Path(__file__).parents[1] / 'database_migrations.py').read_text('utf-8')
        self.assertEqual(migration_cleanup_violations(source), [])

    def test_refreshing_hash_does_not_hide_cleanup_regressions(self):
        source = (Path(__file__).parents[1] / 'database_migrations.py').read_text('utf-8')
        changes = [
            ('                connection.close()', '                pass'),
            ('                connection.close()', '                if not mysql_lock:\n                    connection.close()'),
            ('            if failure is release_error:\n                raise', '            if failure is release_error:\n                pass'),
            ('                connection.invalidate()', '                pass'),
            ('            if mysql_lock:', '            if not mysql_lock:'),
            ('                connection.commit()\n        except BaseException', '                pass\n        except BaseException'),
            ('                connection.close()', '                space.migration_engine.dispose()'),
            ('        failure = error', '        failure = None'),
            ('    failure = None', "    failure = RuntimeError('premature')"),
            ("failure.add_note('Migration rollback failed: ' + type(rollback_error).__name__)", 'raise'),
            ("failure.add_note('Migration invalidation failed: ' + type(invalidate_error).__name__)", 'raise'),
            ("failure.add_note('Migration close failed: ' + type(close_error).__name__)", 'raise'),
        ]
        binding = {'path': 'database_migrations.py', 'language': 'python', 'symbol': 'migration_connection'}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / binding['path']
            path.write_text(source, encoding='utf-8')
            original = source_digest(root, binding)
            for old, new in changes:
                with self.subTest(old=old, new=new):
                    self.assertEqual(source.count(old), 1)
                    mutant = source.replace(old, new, 1)
                    path.write_text(mutant, encoding='utf-8')
                    refreshed = {**binding, 'sha256': source_digest(root, binding)}
                    self.assertNotEqual(refreshed['sha256'], original)
                    self.assertEqual(refreshed['sha256'], source_digest(root, refreshed))
                    self.assertTrue(migration_cleanup_violations(mutant))

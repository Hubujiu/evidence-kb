import ast
from copy import deepcopy
import json
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_project


ROOT = Path(__file__).resolve().parents[2]


class OfflineBootstrapOrderContracts(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / 'scripts/check_project.py').read_text('utf-8')
        self.tree = ast.parse(self.source)

    def definition(self, tree, name):
        return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_current_order_and_unrelated_postseal_edits_are_accepted(self):
        self.assertEqual([], check_project.offline_bootstrap_violations(self.tree))
        changed = deepcopy(self.tree)
        self.definition(changed, 'run_worker').body.append(ast.Pass())
        self.assertEqual([], check_project.offline_bootstrap_violations(changed))
        self.assertEqual([], check_project.offline_bootstrap_violations(ast.parse(self.source.replace(
            'machine = platform.machine()', 'machine=(platform.machine())'))))

    def test_missing_or_reordered_metadata_and_audit_are_rejected(self):
        for mutation in ('missing-metadata', 'missing-audit', 'audit-before-metadata', 'swallow-error'):
            changed = deepcopy(self.tree)
            helper = self.definition(changed, 'seal_offline_worker')
            if mutation == 'missing-metadata':
                helper.body.pop(0)
            elif mutation == 'missing-audit':
                helper.body.pop(1)
            elif mutation == 'audit-before-metadata':
                helper.body[0], helper.body[1] = helper.body[1], helper.body[0]
            else:
                helper.body = [ast.Try(body=helper.body, handlers=[ast.ExceptHandler(type=None, name=None,
                    body=[ast.Pass()])], orelse=[], finalbody=[])]
            with self.subTest(mutation=mutation):
                self.assertTrue(check_project.offline_bootstrap_violations(changed))

    def test_project_import_or_discovery_before_seal_is_rejected(self):
        statements = ('from kb_platform import core', 'import sqlalchemy',
                      "unittest.defaultTestLoader.loadTestsFromName('kb_platform.rag_testing.quality_contracts')")
        for statement in statements:
            changed = deepcopy(self.tree)
            self.definition(changed, 'run_worker').body.insert(2, ast.parse(statement).body[0])
            with self.subTest(statement=statement):
                self.assertTrue(check_project.offline_bootstrap_violations(changed))
        for statement in ('import sqlalchemy', "__import__('sqlalchemy')", 'if True:\n    import sqlalchemy'):
            changed = deepcopy(self.tree)
            changed.body.insert(1, ast.parse(statement).body[0])
            with self.subTest(startup=statement):
                self.assertTrue(check_project.offline_bootstrap_violations(changed))

    def test_bootstrap_cannot_preimport_sqlalchemy_or_project_code(self):
        for statement in ('import sqlalchemy', 'from kb_platform import core'):
            changed = deepcopy(self.tree)
            self.definition(changed, 'seal_offline_worker').body.insert(0, ast.parse(statement).body[0])
            with self.subTest(statement=statement):
                self.assertTrue(check_project.offline_bootstrap_violations(changed))
        changed = deepcopy(self.tree)
        helper = self.definition(changed, 'seal_offline_worker')
        helper.args = ast.parse("def seal_offline_worker(*, eager=__import__('sqlalchemy')): pass").body[0].args
        self.assertTrue(check_project.offline_bootstrap_violations(changed))

    def test_unsealed_coordinator_cannot_discover_contracts(self):
        changed = deepcopy(self.tree)
        self.definition(changed, 'run_contract_workers').body.insert(0, ast.parse(
            "unittest.defaultTestLoader.loadTestsFromName('kb_platform.rag_testing.quality_contracts')").body[0])
        self.assertTrue(check_project.offline_bootstrap_violations(changed))


class OfflineBootstrapRuntimeContracts(unittest.TestCase):
    def test_real_seal_preserves_machine_metadata_and_denies_process_and_network_events(self):
        observed = platform.machine()
        self.assertEqual(observed, check_project.seal_offline_worker())
        for event in ('subprocess.Popen', 'socket.connect', 'socket.getaddrinfo', 'socket.sendto',
                      'socket.sendmsg', 'os.system', 'os.posix_spawn', 'os.exec'):
            with self.subTest(event=event), self.assertRaisesRegex(RuntimeError, 'OFFLINE_CHECK_FORBIDS_'):
                sys.audit(event, None)

    def test_synthetic_cold_metadata_fallback_occurs_only_before_seal(self):
        observed = platform.machine()
        events = []
        ready = False
        sealed = False

        def metadata():
            nonlocal ready
            if not ready:
                self.assertFalse(sealed)
                events.append('OS_METADATA_PROBE')
                ready = True
            return observed

        def install(hook):
            nonlocal sealed
            self.assertTrue(ready)
            self.assertIs(check_project.deny_external_io, hook)
            events.append('SEALED')
            sealed = True

        with patch.object(check_project.platform, 'machine', side_effect=metadata), \
                patch.object(check_project.sys, 'addaudithook', side_effect=install):
            self.assertEqual(observed, check_project.seal_offline_worker())
            self.assertEqual(observed, metadata())
        self.assertEqual(['OS_METADATA_PROBE', 'SEALED'], events)

    def test_discovery_begins_only_after_seal_and_observed_metadata_is_archived(self):
        observed = platform.machine()
        events = []

        def install(hook):
            self.assertIs(check_project.deny_external_io, hook)
            events.append('SEALED')

        def discover():
            self.assertEqual(['SEALED'], events)
            events.append('DISCOVER')
            raise RuntimeError('synthetic discovery boundary')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(check_project.sys, 'path', list(sys.path)), \
                patch.object(check_project.sys, 'addaudithook', side_effect=install), \
                patch.object(check_project, 'contract_modules', side_effect=discover), \
                patch('scripts.check_contract_watchdog.SnapshotWatchdog') as watchdog:
            watchdog.return_value.cancel.return_value = {
                'thread_stopped': True, 'error': None, 'written': False, 'due': False, 'triggered': False}
            with self.assertRaisesRegex(RuntimeError, 'synthetic discovery boundary'):
                check_project.run_worker('contracts', Path(directory), 0)
            record = json.loads((Path(directory) / 'worker-bootstrap.json').read_text('utf-8'))
            self.assertEqual(observed, record['platform_machine'])
            self.assertEqual(['BOOTSTRAP', 'SEALED', 'DISCOVER'], record['states'])
        self.assertEqual(['SEALED', 'DISCOVER'], events)

    def test_metadata_or_seal_failure_never_starts_discovery(self):
        for failed_stage in ('metadata', 'seal'):
            with self.subTest(stage=failed_stage), tempfile.TemporaryDirectory() as directory, \
                    patch.object(check_project.sys, 'path', list(sys.path)), \
                    patch.object(check_project.platform, 'machine', side_effect=(
                        OSError('metadata failed') if failed_stage == 'metadata' else None), return_value=platform.machine()), \
                    patch.object(check_project.sys, 'addaudithook', side_effect=(
                        RuntimeError('seal failed') if failed_stage == 'seal' else None)) as seal, \
                    patch.object(check_project, 'contract_modules') as discover:
                with self.assertRaises((OSError, RuntimeError)):
                    check_project.run_worker('contracts', Path(directory), 0)
                discover.assert_not_called()
                if failed_stage == 'metadata':
                    seal.assert_not_called()
                self.assertFalse((Path(directory) / 'worker-bootstrap.json').exists())

import argparse
import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import delivery_control


class DeliveryControlContracts(unittest.TestCase):
    def test_real_delivery_control_fixture_is_in_mandatory_foundation_suite(self):
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / 'kb_platform/rag_testing/foundation_integration.py').read_text('utf-8'))
        suite = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'integration_suite')
        loops = [node for node in ast.walk(suite) if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)]
        self.assertEqual(sum(isinstance(item, ast.Name) and item.id == 'delivery_control_integration'
                             for loop in loops for item in loop.iter.elts), 1)

    def test_check_only_validates_without_starting_a_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('.venv/bin/python', 'config.json', 'providers.json', 'semantic.json'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            args = argparse.Namespace(config='config.json', providers='providers.json', semantic_runtime='semantic.json',
                                      run_root='runs', budget_batch='', check_only=True)
            with patch.object(delivery_control, 'ROOT', root), patch.object(delivery_control.subprocess, 'run') as check, \
                    patch.object(delivery_control.subprocess, 'Popen') as spawn:
                check.return_value.returncode = 0
                delivery_control.start(args)
            self.assertIn('--check', check.call_args.args[0])
            self.assertEqual(check.call_args.args[0][0], str(root / '.venv/bin/python'))
            spawn.assert_not_called()
            self.assertFalse((root / 'runs').exists())

    def test_stop_rejects_marker_outside_recorded_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            run.mkdir()
            receipt = run / 'process.json'
            receipt.write_text(json.dumps({'run': str(run), 'stop_file': str(root / 'stop.request')}))
            args = argparse.Namespace(receipt=str(receipt), timeout_seconds=1)
            with patch.object(delivery_control.os, 'pidfd_open', create=True) as open_pid:
                with self.assertRaisesRegex(ValueError, 'recorded server run'):
                    delivery_control.stop(args)
            open_pid.assert_not_called()
            self.assertFalse((root / 'stop.request').exists())

    def test_stop_rejects_reused_pid_before_writing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / 'process.json'
            receipt.write_text(json.dumps({'run': str(root), 'stop_file': str(root / 'stop.request'), 'pid': 123,
                                           'process_start': 'old', 'host_binary': str(root / 'host')}))
            (root / 'host').touch()
            args = argparse.Namespace(receipt=str(receipt), timeout_seconds=1)
            resolve = Path.resolve

            def fake_resolve(path, *args, **kwargs):
                if path.parts[-3:] == ('proc', '123', 'exe'):
                    return root / 'host'
                return resolve(path, *args, **kwargs)

            with patch.object(delivery_control.os, 'pidfd_open', create=True, return_value=5), \
                    patch.object(delivery_control.os, 'close'), patch.object(delivery_control.select, 'poll', create=True) as poll, \
                    patch.object(delivery_control.select, 'POLLIN', 1, create=True), \
                    patch.object(delivery_control, 'process_start', return_value='new'), \
                    patch.object(Path, 'resolve', fake_resolve):
                poll.return_value.poll.return_value = []
                with self.assertRaisesRegex(ValueError, 'different process'):
                    delivery_control.stop(args)
            self.assertFalse((root / 'stop.request').exists())

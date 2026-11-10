import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .formal_state_machine_contracts import sample_log
from .formal_state_machines import run_inline_export, run_model
from .formal_tool_isolation import formal_tool_isolation_violations


ROOT = Path(__file__).resolve().parents[2]
OWNER = 'kb_platform/rag_testing/formal_state_machines.py'


class FormalToolIsolationContracts(unittest.TestCase):
    def fixture(self, root):
        formal = root / 'formal'
        formal.mkdir()
        (formal / 'Mini.tla').write_text('---- MODULE Mini ----\nEXTENDS Naturals\n====\n', encoding='utf-8')
        (formal / 'Mini.cfg').write_text('SPECIFICATION Spec\nINVARIANT Safe\n', encoding='utf-8')
        return dict(id='run', kind='positive', module='formal/Mini.tla', config='formal/Mini.cfg',
                    heap_mb=256, timeout_seconds=30)

    def invoke(self, commands, *, xml=False):
        def execute(command, **kwargs):
            commands.append(command)
            flags = [value for value in command if value.startswith('-Djava.io.tmpdir=')]
            self.assertEqual(len(flags), 1)
            scratch = Path(flags[0].split('=', 1)[1])
            self.assertTrue(scratch.is_absolute())
            self.assertTrue(scratch.is_dir())
            self.assertEqual(scratch.name, 'java-tmp')
            self.assertLess(command.index(flags[0]), command.index('-cp'))
            (scratch / 'Naturals.tla').write_text('owned module', encoding='utf-8')
            kwargs['stdout'].write(b'<xml/>' if xml else sample_log())
            return SimpleNamespace(returncode=0)
        return execute

    def test_model_launch_uses_exclusive_absolute_scratch_even_for_relative_output(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self.fixture(root)
            output = root / 'output'
            output.mkdir()
            relative = Path(os.path.relpath(output, Path.cwd()))
            commands = []
            with patch('kb_platform.rag_testing.formal_state_machines.subprocess.run',
                       side_effect=self.invoke(commands)):
                result = run_model(root, root / 'unused.jar', relative, spec)
            self.assertEqual(result['decision'], 'PASS')
            self.assertEqual(result['command'], commands[0])
            self.assertIn('-Djava.io.tmpdir=' + str(output / 'run/java-tmp'), commands[0])

    def test_inline_export_uses_the_same_exclusive_absolute_scratch_contract(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            output = root / 'output'
            output.mkdir()
            relative = Path(os.path.relpath(output, Path.cwd()))
            commands = []
            with patch('kb_platform.rag_testing.formal_state_machines.subprocess.run',
                       side_effect=self.invoke(commands, xml=True)), \
                    patch('kb_platform.rag_testing.formal_state_machines.inline_boolean_findings', return_value=[]):
                result = run_inline_export(root / 'unused.jar', relative, root / 'formal', 'export', 'Mini', {}, [])
            self.assertEqual(result['decision'], 'PASS')
            self.assertIn('-Djava.io.tmpdir=' + str(output / 'export/java-tmp'), commands[0])

    def test_independent_executions_cannot_delete_each_others_standard_modules(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self.fixture(root)
            commands = []
            for name in ('first', 'second'):
                output = root / name
                output.mkdir()
                with patch('kb_platform.rag_testing.formal_state_machines.subprocess.run',
                           side_effect=self.invoke(commands)):
                    self.assertEqual(run_model(root, root / 'unused.jar', output, spec)['decision'], 'PASS')
            first, second = [Path(next(flag.split('=', 1)[1] for flag in command
                                     if flag.startswith('-Djava.io.tmpdir='))) for command in commands]
            self.assertNotEqual(first, second)
            (first / 'Naturals.tla').unlink()
            self.assertEqual((second / 'Naturals.tla').read_text('utf-8'), 'owned module')

    def test_actual_tool_owner_matches_isolation_lint(self):
        self.assertEqual(formal_tool_isolation_violations((ROOT / OWNER).read_text('utf-8')), [])

    def test_missing_shared_relative_or_reassigned_java_scratch_is_rejected_without_hashes(self):
        original = (ROOT / OWNER).read_text('utf-8')
        mutations = [
            ("f'-Djava.io.tmpdir={scratch}', ", ''),
            ("scratch = (directory / 'java-tmp').resolve()", "scratch = Path('/tmp')"),
            ("scratch = (directory / 'java-tmp').resolve()", "scratch = directory / 'java-tmp'"),
            ("scratch.mkdir()", "scratch.mkdir(exist_ok=True)"),
            ("scratch.mkdir()", "scratch.mkdir()\n    scratch = Path('/tmp')"),
            ("f'-Djava.io.tmpdir={scratch}'", "'-Djava.io.tmpdir=/tmp'"),
        ]
        for name in ('run_model', 'run_inline_export'):
            start = original.index('def ' + name + '(')
            stop = original.index('\ndef ', start + 4)
            region = original[start:stop]
            for before, after in mutations:
                with self.subTest(owner=name, mutation=before):
                    self.assertIn(before, region)
                    changed = original[:start] + region.replace(before, after, 1) + original[stop:]
                    self.assertTrue(formal_tool_isolation_violations(changed))

import ast
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.host_process_lint import PATHS, host_source_violations


ROOT = Path(__file__).resolve().parents[2]


class HostProcessFormalContracts(unittest.TestCase):
    def sources(self):
        return {path: (ROOT / path).read_text('utf-8') for path in PATHS}

    def test_actual_host_owners_match_reviewed_structure(self):
        self.assertEqual(host_source_violations(self.sources()), [])

    def test_seeded_early_cleanup_wrong_correlation_and_replay_mutants_are_rejected(self):
        sources = self.sources()
        mutants = [
            (PATHS[0], 'self._stop_done.wait()', 'pass'),
            (PATHS[0], 'self._settled.wait()', 'pass'),
            (PATHS[1], "self._pending.get(frame['id'])", 'next(iter(self._pending.values()), None)'),
            (PATHS[2], 'write.sent = True', 'write.sent = False'),
            (PATHS[2], 'sent = write.sent', 'sent = False'),
            (PATHS[2], "self._record(request, 'UNKNOWN',", "self._record(request, 'NOT_SENT',"),
            (PATHS[2], '            self._enqueue(write)', '            self._enqueue(write)\n            self._enqueue(write)'),
            (PATHS[2], 'self._reader.join(timeout=IO_STOP_SECONDS)', 'pass'),
            (PATHS[3], '(Ok(_), Err(error)) => Err(HostError::new("worker_cleanup", error.to_string())),', '(Ok(value), Err(_)) => Ok(value),'),
            (PATHS[4], 'let joined = join(&mut workers);', 'let joined = Ok(());'),
            (PATHS[5], 'self.child.try_wait()?.is_some() && self.group.is_empty()?', 'true'),
            (PATHS[7], 'accounting.ActiveProcesses == 0', 'true'),
        ]
        for path, old, new in mutants:
            with self.subTest(path=path, mutation=old):
                self.assertIn(old, sources[path])
                source = sources[path]
                start = source.index('    def request(') if path == PATHS[2] and old in {
                    'sent = write.sent', '            self._enqueue(write)'} else 0
                changed = {**sources, path: source[:start] + source[start:].replace(old, new, 1)}
                self.assertTrue(host_source_violations(changed))

    def test_mandatory_semantic_guard_rejects_refreshed_hashes(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(row for row in registry['machines'] if row['id'] == 'host_process')
        registry = {'schema_version': 1, 'discovery': {'roots': ['kb_platform', 'rust'], 'exclusions': []},
                    'machines': [machine]}
        paths = set(PATHS)
        paths.update(row[key] for row in machine['models'] + machine['negative_controls'] for key in ('module', 'config'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            self.assertEqual(registry_violations(root, registry), [])
            path = root / PATHS[0]
            path.write_text(path.read_text('utf-8').replace('self._stop_done.wait()', 'pass', 1), 'utf-8')
            for binding in machine['source_bindings']:
                binding['sha256'] = source_digest(root, binding)
            errors = registry_violations(root, registry)
            self.assertTrue(any('actual run/close settlement' in error for error in errors))
            self.assertFalse(any('source review drift' in error for error in errors))

    def test_physical_knowledge_regression_is_on_the_mandatory_process_integration_path(self):
        tree = ast.parse((ROOT / 'kb_platform/rag_testing/plugin_process_integration.py').read_text('utf-8'))
        run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
        calls = [node for node in ast.walk(run) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'verify_physical_knowledge']
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0]), 'verify_physical_knowledge(root)')
        self.assertIn("'physical_knowledge': physical_knowledge", ast.unparse(run))

import ast
from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.remote_retry_lint import OWNER, SDK, remote_retry_source_violations


ROOT = Path(__file__).resolve().parents[2]
MUTANTS = [
    (OWNER, '_Transport._post', 'failure.retryable and attempt < self.retries', 'attempt < self.retries'),
    (OWNER, '_Transport._post', 'waited + delay > self.max_retry_wait_seconds', 'False'),
    (OWNER, '_Transport._post', "previous_id = trace.get('last_physical_request_id')", 'previous_id = None'),
    (OWNER, '_Transport._post', "trace['attempt'] -= 1", 'pass'),
    (OWNER, 'LunaClient._complete', 'format_attempts = 2', 'format_attempts = 3'),
    (OWNER, 'LunaClient._complete', 'or isinstance(exc, TerminalContractError)', 'or False'),
    (OWNER, 'LunaClient._complete', 'payload["messages"][1]', '{"role": "user", "content": "changed"}'),
    (OWNER, 'GoogleEmbeddingProvider._post', 'while True:', 'while True:\n            state.retries_used = 0'),
    (OWNER, 'GoogleEmbeddingProvider._post', 'state.retries_used < self.retries', 'True'),
    (OWNER, 'GoogleEmbeddingProvider._embed', 'trace.auth_refreshes = 1', 'trace.auth_refreshes = 0'),
    (OWNER, 'GoogleEmbeddingProvider._embed', 'with self._vertex_slots:', 'with self._vertex_slots:\n                    self._vertex_slots.__exit__(None, None, None)'),
    (OWNER, 'GoogleEmbeddingProvider._vertex_headers', 'current == rejected_authorization', 'True'),
    (OWNER, '_GoogleRequest.metadata', '"auth_refreshes": self.auth_refreshes', '"auth_refreshes": self.attempt'),
    (OWNER, '_GoogleRequestPolicy.wait', 'state.wait_seconds + now - started + delay', 'delay'),
    (OWNER, '_GoogleRequestPolicy.wait', 'self._sleep(delay)', 'self._sleep(0)'),
    (OWNER, '_GoogleRequestPolicy.defer', 'max(self._cooldown_until, self._clock() + delay)', 'min(self._cooldown_until, self._clock() + delay)'),
    (OWNER, '_GoogleRequestPolicy.defer', 'max(delay, retry_after)', 'min(delay, retry_after)'),
    (OWNER, '_FifoSlots.__enter__', 'self._waiters[0] is not token or ', ''),
    (OWNER, '_FifoSlots.__enter__', 'self._waiters.remove(token)', 'pass'),
    (OWNER, '_FifoSlots.__enter__', 'self._available -= 1', 'self._available += 1'),
    (SDK, 'chat_response', 'max_retries=0', 'max_retries=2'),
]


def mutate(source, symbol, before, after):
    tree = ast.parse(source)
    parts = symbol.split('.')
    owner = next(node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == parts[0])
    if len(parts) == 2:
        owner = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == parts[1])
    lines = source.splitlines(keepends=True)
    region = ''.join(lines[owner.lineno - 1:owner.end_lineno])
    if before not in region:
        raise AssertionError(f'Mutant target absent: {symbol}: {before}')
    return ''.join(lines[:owner.lineno - 1]) + region.replace(before, after, 1) + ''.join(lines[owner.end_lineno:])


class RemoteRetryFormalContracts(unittest.TestCase):
    def test_actual_remote_retry_owner_and_sdk_match_reviewed_structure(self):
        self.assertEqual(remote_retry_source_violations((ROOT / OWNER).read_text(), (ROOT / SDK).read_text()), [])

    def test_seeded_transport_auth_format_wait_slot_and_sdk_mutants_are_rejected(self):
        originals = {path: (ROOT / path).read_text() for path in (OWNER, SDK)}
        for path, symbol, before, after in MUTANTS:
            with self.subTest(owner=symbol, mutation=before):
                sources = dict(originals)
                sources[path] = mutate(sources[path], symbol, before, after)
                self.assertTrue(remote_retry_source_violations(sources[OWNER], sources[SDK]))

    def test_mandatory_structural_guards_reject_seeded_changes_after_all_hashes_refresh(self):
        machine = next(row for row in json.loads((ROOT / 'formal/registry-v1.json').read_text())['machines']
                       if row['id'] == 'remote_retry_ownership')
        paths = {row['path'] for row in machine['source_bindings']}
        paths.update(row[key] for row in machine['models'] + machine['negative_controls'] for key in ('module', 'config'))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            originals = {path: (root / path).read_text() for path in (OWNER, SDK)}
            for path, symbol, before, after in (MUTANTS[index] for index in (0, 4, 7, 10, 11, 15, 20)):
                with self.subTest(owner=symbol):
                    for name, source in originals.items():
                        (root / name).write_text(source)
                    (root / path).write_text(mutate(originals[path], symbol, before, after))
                    reviewed = deepcopy(machine)
                    for binding in reviewed['source_bindings']:
                        binding['sha256'] = source_digest(root, binding)
                    registry = {'schema_version': 1, 'discovery': {'roots': [], 'exclusions': []}, 'machines': [reviewed]}
                    errors = registry_violations(root, registry)
                    self.assertTrue(any(error.startswith('remote_retry_ownership:') for error in errors))
                    self.assertFalse(any('source review drift' in error for error in errors))

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.llm_stream_lint import stream_source_violations


ROOT = Path(__file__).resolve().parents[2]
SERVICE = 'rust/llm-service/src/service.rs'
ASSEMBLER = 'rust/llm-service/src/assembler.rs'


class LlmStreamFormalContracts(unittest.TestCase):
    def test_common_terminal_snapshot_and_real_task_ownership_structure(self):
        self.assertEqual(stream_source_violations((ROOT / SERVICE).read_text('utf-8'),
                                                 (ROOT / ASSEMBLER).read_text('utf-8')), [])

    def test_seeded_snapshot_sum_early_release_and_content_limit_mutants(self):
        service, assembler = ((ROOT / path).read_text('utf-8') for path in (SERVICE, ASSEMBLER))
        mutants = [
            (service.replace('self.terminal_usage = observed.clone();', '', 1), assembler),
            (service.replace('if self.assembler.finished() {',
                             'if matches!(event, StreamEvent::Finish { .. }) {', 1), assembler),
            (service.replace('response.usage = Some(usage.clone());',
                             'response.usage = Some(TokenUsage::default());', 1), assembler),
            (service.replace('response.usage = Some(usage.clone());',
                             'response.usage = Some(TokenUsage { input_tokens: usage.input_tokens + 1, ..usage.clone() });', 1), assembler),
            (service.replace('self.done = true;', 'self.task = None; self.done = true;', 1), assembler),
            (service.replace('})?;\n            self.task = None;',
                             '});\n            self.task = None;', 1), assembler),
            (service, assembler.replace('if serde_json::to_vec(self).map_or(true, |v| v.len() > MAX_CONTENT_BYTES)',
                                        'if false', 1)),
        ]
        for changed_service, changed_assembler in mutants:
            self.assertNotEqual((changed_service, changed_assembler), (service, assembler))
            self.assertTrue(stream_source_violations(changed_service, changed_assembler))

    def test_mandatory_registry_lint_rejects_regression_even_with_refreshed_source_hash(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(item for item in registry['machines'] if item['id'] == 'llm_stream')
        registry = {'schema_version': 1, 'discovery': {'roots': ['rust'], 'exclusions': []}, 'machines': [machine]}
        paths = {SERVICE, ASSEMBLER}
        paths.update(run[key] for run in machine['models'] + machine['negative_controls']
                     for key in ('module', 'config'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            self.assertEqual(registry_violations(root, registry), [])
            path = root / SERVICE
            path.write_text(path.read_text('utf-8').replace('self.terminal_usage = observed.clone();', '', 1), 'utf-8')
            for binding in machine['source_bindings']:
                binding['sha256'] = source_digest(root, binding)
            errors = registry_violations(root, registry)
            self.assertTrue(any('common terminal publication' in error for error in errors))
            self.assertFalse(any('source review drift' in error for error in errors))

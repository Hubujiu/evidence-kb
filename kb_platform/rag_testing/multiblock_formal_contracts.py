import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.multiblock_lint import multiblock_source_violations

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'rust/llm-service/src/assembler.rs'
MODEL = 'formal/llm_multiblock/MultiblockAssembler.tla'


class MultiblockFormalContracts(unittest.TestCase):
    def test_reviewed_multiblock_seams(self):
        self.assertEqual(multiblock_source_violations((ROOT / SOURCE).read_text('utf-8'), (ROOT / MODEL).read_text('utf-8')), [])

    def test_semantic_guard_rejects_refreshed_hash_regressions(self):
        original = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(m for m in original['machines'] if m['id'] == 'llm_multiblock')
        registry = {'schema_version': 1, 'discovery': {'roots': ['rust'], 'exclusions': []}, 'machines': [machine]}
        paths = {SOURCE} | {r[k] for r in machine['models'] + machine['negative_controls'] for k in ('module', 'config')}
        mutations = [
            ('let mut next = self.clone();', 'let mut next = Self::default();'),
            ('block.kind != kind || block.closed.is_some()', 'block.kind != kind'),
            ('!ids.insert(id)', 'false'),
            ('v.is_object()', 'true'),
            ('(!active.id.is_empty() && active.id != *id)', 'false'),
            ('blocks.len() != kept.len()', 'false'),
            ('keep.then_some(block.clone())', 'Some(block.clone())'),
            ('block.is_safe_text()', 'true'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            self.assertEqual(registry_violations(root, registry), [])
            source = (ROOT / SOURCE).read_text('utf-8')
            for before, after in mutations:
                with self.subTest(before=before):
                    changed = source.replace(before, after, 1)
                    self.assertNotEqual(changed, source)
                    (root / SOURCE).write_text(changed, 'utf-8')
                    revised = copy.deepcopy(registry)
                    for binding in revised['machines'][0]['source_bindings']:
                        binding['sha256'] = source_digest(root, binding)
                    errors = registry_violations(root, revised)
                    self.assertTrue(any('llm_multiblock:' in error for error in errors), errors)
                    self.assertFalse(any('source review drift' in error for error in errors))

    def test_model_rollback_observes_fields_not_only_a_ghost_boolean(self):
        source = (ROOT / SOURCE).read_text('utf-8')
        model = (ROOT / MODEL).read_text('utf-8')
        for before, after in [
            ('RejectedUnchanged == rejected => <<filled,kind,ids,valid,closed,order>> = rejectedBefore', 'RejectedUnchanged == TRUE'),
            ('THEN [kind EXCEPT ![i] = IF @ = "text" THEN "reasoning" ELSE "text"]', 'THEN kind'),
        ]:
            changed = model.replace(before, after, 1)
            self.assertNotEqual(changed, model)
            self.assertTrue(multiblock_source_violations(source, changed))

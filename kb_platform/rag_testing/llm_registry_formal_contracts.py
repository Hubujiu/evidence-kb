import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.llm_registry_lint import registry_source_violations


ROOT = Path(__file__).resolve().parents[2]
PATHS = ('rust/llm-service/src/registry.rs', 'rust/llm-service/src/executor.rs', 'kb_platform/model_service.py')


class LlmRegistryFormalContracts(unittest.TestCase):
    def sources(self):
        return tuple((ROOT / path).read_text('utf-8') for path in PATHS)

    def test_actual_registry_and_managed_driver_ownership_structure(self):
        self.assertEqual(registry_source_violations(*self.sources()), [])

    def test_seeded_binding_cleanup_and_early_release_mutants_are_rejected(self):
        registry, executor, driver = self.sources()
        mutants = [
            (registry.replace('self.inner.current(&state, self.snapshot.binding)?;', '', 1), executor, driver),
            (registry.replace('entry.phase == RoutePhase::Cleaning', 'false', 1), executor, driver),
            (registry.replace('entry.phase = RoutePhase::CleanupFailed;', 'entry.phase = RoutePhase::Active;', 1), executor, driver),
            (registry.replace('state.phase = ServicePhase::Draining;', '', 1), executor, driver),
            (registry.replace('binding.generation == entry.generation', 'true', 1), executor, driver),
            (registry, executor.replace('.get_mut(&call)', '.remove(&call)', 1), driver),
            (registry, executor.replace('service.prepared.remove(&prepared)', 'service.prepared.get(&prepared)', 1), driver),
            (registry, executor, driver.replace("error=type(error).__name__", 'error=None', 1)),
            (registry, executor, driver.replace("            del self._adapters[binding['registration']]", '', 1)),
        ]
        for mutant in mutants:
            self.assertNotEqual(mutant, self.sources())
            self.assertTrue(registry_source_violations(*mutant))

    def test_mandatory_guard_rejects_refreshed_hash_for_a_stale_binding_mutation(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(item for item in registry['machines'] if item['id'] == 'llm_registry')
        registry = {'schema_version': 1, 'discovery': {'roots': ['rust', 'kb_platform'], 'exclusions': []}, 'machines': [machine]}
        paths = set(PATHS)
        paths.update(run[key] for run in machine['models'] + machine['negative_controls'] for key in ('module', 'config'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            self.assertEqual(registry_violations(root, registry), [])
            path = root / PATHS[0]
            path.write_text(path.read_text('utf-8').replace('binding.generation == entry.generation', 'true', 1), 'utf-8')
            for binding in machine['source_bindings']:
                binding['sha256'] = source_digest(root, binding)
            errors = registry_violations(root, registry)
            self.assertTrue(any('exact current binding' in error for error in errors))
            self.assertFalse(any('source review drift' in error for error in errors))

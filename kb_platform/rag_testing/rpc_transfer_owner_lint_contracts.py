from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.rpc_transfer_owner_checks import WITNESSES, rpc_transfer_owner_violations
from . import formal_state_machine_contracts as fixtures
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]
OWNERS = {'platform_upload_snapshots', 'auxiliary_ocr_upload'}


class RPCTransferOwnerLintContracts(unittest.TestCase):
    def fixture(self, root):
        registry = fixtures.FormalRegistryContracts().fixture(root)
        registry['discovery']['roots'] = []
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        for machine in actual['machines']:
            if machine['id'] not in OWNERS:
                continue
            registry['machines'].append(deepcopy(machine))
            paths = {binding['path'] for binding in machine['source_bindings']}
            paths.update(row[key] for row in machine['models'] + machine['negative_controls']
                         for key in ('module', 'config'))
            for path in paths:
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / path).read_bytes())
        return registry

    def refresh(self, root, registry):
        for machine in registry['machines']:
            for binding in machine['source_bindings']:
                binding['sha256'] = source_digest(root, binding)

    def test_registered_models_and_real_owner_semantics_pass(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        self.assertEqual(rpc_transfer_owner_violations(ROOT, registry), [])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(registry_violations(root, self.fixture(root)), [])

    def test_refreshed_source_hash_cannot_hide_ownership_mutations(self):
        platform = 'kb_platform/knowledge_platform_contract.py'
        snapshots = 'kb_platform/platform_transfers.py'
        ocr = 'kb_platform/knowledge_model_ports.py'
        mutations = (
            (platform, "len(content) != expected['size'] or sha256(content).hexdigest() != expected['sha256']",
             "len(content) != expected['size']", 'complete length and hash'),
            (platform, "identity = self._transfers.reserve(request['actor'], 'ingest')\n            del self._uploads[request['upload_id']]",
             "del self._uploads[request['upload_id']]\n            identity = self._transfers.reserve(request['actor'], 'ingest')", 'reserve result capacity'),
            (ocr, "del self._uploads[request['upload_id']]", 'content = bytes(content)', 'complete length and hash'),
            (ocr, "len(content) != expected['image_size'] or sha256(content).hexdigest() != expected['image_sha256']",
             "len(content) != expected['image_size']", 'complete length and hash'),
            (ocr, '            validate_ocr_response(result)', '            result = dict(result)', 'validate provider result'),
            (snapshots, 'is not snapshot:', '!= snapshot:', 'identical snapshot'),
            (snapshots, 'offset != snapshot.stream.tell()', 'offset < 0', 'next exact integer offset'),
            (snapshots, 'snapshot.actor != actor', 'False', 'bind stream, actor and kind'),
            (snapshots, 'self._closed = True', 'self._closed = False', 'close admission'),
            (snapshots, 'except BaseException:', 'except Exception:', 'failed publication'),
        )
        for path, before, after, expected in mutations:
            with self.subTest(expected=expected, path=path), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                file = root / path
                self.assertIn(before, file.read_text('utf-8'))
                file.write_text(file.read_text('utf-8').replace(before, after), encoding='utf-8')
                self.refresh(root, registry)
                errors = registry_violations(root, registry)
                self.assertTrue(any(expected in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)

    def test_terminal_witnesses_cannot_be_initial_only_missing_or_fault_enabled(self):
        for mutation in ('initial-only', 'missing-registration', 'fault-enabled', 'positive-constraint'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                name, witness = 'OCRUpload', 'Returned'
                module = root / f'formal/rpc_transfer_owners/{name}.tla'
                if mutation == 'initial-only':
                    old = f'No{witness}Witness == ~({WITNESSES[name][witness]})'
                    module.write_text(module.read_text().replace(old, 'NoReturnedWitness == ~(closed = FALSE)'))
                elif mutation == 'missing-registration':
                    for machine in registry['machines']:
                        machine['negative_controls'] = [row for row in machine['negative_controls']
                            if row.get('expected_invariant') != 'NoReturnedWitness']
                elif mutation == 'fault-enabled':
                    config = root / f'formal/rpc_transfer_owners/{name}{witness}Witness.cfg'
                    config.write_text(config.read_text().replace('CheckIntegrity = TRUE', 'CheckIntegrity = FALSE'))
                else:
                    config = root / f'formal/rpc_transfer_owners/{name}.cfg'
                    config.write_text(config.read_text() + 'CONSTRAINT TypeOK\n')
                self.refresh(root, registry)
                errors = registry_violations(root, registry)
                self.assertTrue(any('rpc transfer' in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()

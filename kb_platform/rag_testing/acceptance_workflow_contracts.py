from contextlib import contextmanager
from copy import deepcopy
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.database_contracts import DatabaseError
from kb_platform.database_providers.sqlite_guard import sqlite_guard

from . import delivery_http
from .acceptance_workflow_rules import SOURCE, acceptance_workflow_violations
from .contracts import digest
from .delivery_fixtures import VERSION as FIXTURE_VERSION, load_delivery_fixtures
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class AcceptanceOwnershipContracts(unittest.TestCase):
    def test_preparation_holds_real_sqlite_guard_before_loading_any_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def locked(*args, **kwargs):
                with self.assertRaises(DatabaseError) as caught:
                    with sqlite_guard(root / '.prepare.guard.sqlite3'):
                        self.fail('second owner admitted')
                self.assertEqual(caught.exception.code, 'DATABASE_GUARD_BUSY')
                return {'owned': True}

            with patch.object(delivery_http, '_prepare_http_locked', side_effect=locked):
                self.assertEqual(delivery_http.prepare_http(None, None, root, platform=None,
                    expected_tenant_id='synthetic', physical_directory=root), {'owned': True})
            with sqlite_guard(root / '.prepare.guard.sqlite3'):
                pass

    def test_rebinding_exclusive_output_also_owns_shared_preparation_guard(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures, previous, output = root / 'fixtures', root / 'previous', root / 'new'
            fixtures.mkdir()
            previous.mkdir()
            (fixtures / 'fixtures.json').write_text(json.dumps({
                'schema_version': FIXTURE_VERSION, 'documents': [], 'cases': []}), 'utf-8')
            identity = {'tenant_id': 'synthetic', 'principal_id': 'owner'}
            fixture_hash = digest({key: value for key, value in load_delivery_fixtures(fixtures).items()
                                   if key != 'root'})
            old = {'protocol': delivery_http.VERSION, 'identity': identity, 'fixture_root': str(fixtures),
                   'file_sha256': {}, 'splits': {}, 'planned_case_ids': [], 'context_budget': 1000}
            (previous / 'prepared.json').write_text(json.dumps(old), 'utf-8')
            (previous / 'prepare-state.json').write_text(json.dumps({'status': 'COMPLETE', 'operations': {},
                'specification': {'fixture_hash': fixture_hash}}), 'utf-8')
            before = {path.name: path.read_bytes() for path in previous.iterdir()}
            client = SimpleNamespace(get=lambda path: SimpleNamespace(
                raise_for_status=lambda: SimpleNamespace(json=lambda: identity)))

            def bind(*args, **kwargs):
                with self.assertRaises(DatabaseError) as caught:
                    with sqlite_guard(output / '.prepare.guard.sqlite3'):
                        self.fail('second owner admitted')
                self.assertEqual(caught.exception.code, 'DATABASE_GUARD_BUSY')
                return {'owned': True}

            with patch.object(delivery_http, '_bind_prepared_http', side_effect=bind):
                self.assertEqual(delivery_http.rebind_http(client, previous, output, platform=None,
                    expected_tenant_id='synthetic', physical_directory=root / 'captures'), {'owned': True})
                with self.assertRaises(FileExistsError):
                    delivery_http.rebind_http(client, previous, output, platform=None,
                        expected_tenant_id='synthetic', physical_directory=root / 'captures')
            self.assertEqual({path.name: path.read_bytes() for path in previous.iterdir()}, before)
            competing_output = root / 'competing-new'
            occupied = competing_output / 'prepare-state.json'

            @contextmanager
            def acquired_after_competitor(path, **options):
                with sqlite_guard(path, **options):
                    occupied.write_bytes(b'competing producer receipt')
                with sqlite_guard(path, **options):
                    yield

            with patch.object(delivery_http, 'sqlite_guard', side_effect=acquired_after_competitor), \
                    patch.object(delivery_http, '_bind_prepared_http') as bind_call:
                with self.assertRaises(FileExistsError):
                    delivery_http.rebind_http(client, previous, competing_output, platform=None,
                        expected_tenant_id='synthetic', physical_directory=root / 'captures')
                bind_call.assert_not_called()
            self.assertEqual(occupied.read_bytes(), b'competing producer receipt')
            self.assertEqual({path.name: path.read_bytes() for path in previous.iterdir()}, before)

    def test_receipt_flush_failure_never_replaces_durable_state(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            target.write_bytes(b'{"status":"STARTED"}')
            with patch.object(delivery_http.os, 'fsync', side_effect=OSError('synthetic flush failure')):
                with self.assertRaisesRegex(OSError, 'synthetic flush'):
                    delivery_http._atomic_json(target, {'status': 'COMPLETE'})
            self.assertEqual(target.read_bytes(), b'{"status":"STARTED"}')
            self.assertEqual([path.name for path in target.parent.iterdir()], ['state.json'])

    def test_semantic_lint_rejects_removed_guard_pending_gate_and_reordered_claim_after_hash_refresh(self):
        source = (ROOT / SOURCE).read_text('utf-8')
        mutations = [
            ("if any(path.name not in {", "if any(False and path.name not in {"),
            ("with sqlite_guard(root / '.prepare.guard.sqlite3', nowait=True):", 'with nullcontext():'),
            ("if record['status'] != 'COMPLETE':", "if record['status'] == 'ERROR':"),
            ("'status': 'STARTED'", "'status': 'ABSENT'"),
            ("        _atomic_json(path, state)\n        try:\n            response = client.request", "        try:\n            response = client.request"),
        ]
        self.assertEqual(acceptance_workflow_violations(ROOT), [])
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(value for value in registry['machines'] if value['id'] == 'acceptance_workflow')
        for old, new in mutations:
            with self.subTest(mutation=new), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / SOURCE
                path.parent.mkdir(parents=True)
                self.assertIn(old, source)
                path.write_text(source.replace(old, new), 'utf-8')
                reviewed = deepcopy(machine)
                for binding in reviewed['source_bindings']:
                    if binding['path'] == SOURCE:
                        binding['sha256'] = source_digest(root, binding)
                for model in reviewed['models'] + reviewed['negative_controls']:
                    for key in ('module', 'config'):
                        destination = root / model[key]
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(ROOT / model[key], destination)
                document = {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
                            'machines': [reviewed]}
                errors = registry_violations(root, document)
                self.assertFalse(any('source review drift' in error for error in errors), errors)
                self.assertTrue(any(error.startswith('acceptance_workflow:') for error in errors), errors)

"""Offline delivery boundary contracts using only temporary synthetic state."""
from copy import deepcopy
import json
import os
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.delivery import (
    DeliveryConfigurationError,
    check_delivery_configuration,
    create_delivery_app,
    ensure_private_identity,
    load_profile,
)
from kb_platform.rag_testing.budget import BudgetLedger


class DeliveryContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.config_path = self.root / 'profile.json'
        self.provider_path = self.run / 'providers.json'
        self.config = {
            'protocol': 'enterprise-kb-delivery/1', 'id': 'delivery-contract',
            'models': {'generation': 'codex/gpt-6-luna',
                       'embedding': 'google_gemini:gemini-embedding-2:768', 'model_comparison': False},
            'window': {'total': 131072, 'input': 118784, 'output': 4096, 'safety': 8192,
                       'counting': 'conservative_utf8_estimate'},
            'scope': {'cross_session_memory': False},
            'admission': {'concurrent_model_requests': 1},
            'physical_call_caps': {'model': 2, 'input_tokens': 1000, 'output_tokens': 100,
                                   'remote_embedding_calls': 2, 'embedding_input_tokens': 1000},
        }
        self.providers = {
            'luna': {'model': 'codex/gpt-6-luna', 'cache_mode': 'disabled', 'retries': 0},
            'google': {'model': 'gemini-embedding-2', 'dimension': 768, 'cache_mode': 'disabled', 'retries': 0},
            'cache_dir': str(self.run / 'cache'), 'log_path': str(self.run / 'calls.jsonl'),
        }
        self.save()
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith('KB_')}
        environment = patch.dict(os.environ, self.environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def save(self):
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.provider_path.write_text(json.dumps(self.providers), encoding='utf-8')

    def profile(self):
        return load_profile(self.config_path, self.provider_path, self.run)

    def check(self):
        return check_delivery_configuration(self.config_path, self.provider_path, self.run)

    def cli(self, *arguments):
        from scripts.delivery_server_app import main

        output = StringIO()
        with patch.object(sys, 'argv', ['delivery_server_app', *arguments]), redirect_stdout(output):
            try:
                code = main()
            except SystemExit as exc:
                code = exc.code
        return SimpleNamespace(returncode=code, stdout=output.getvalue(), stderr='')

    def test_check_reports_profile_without_creating_runtime_state(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        state = self.check()
        self.assertEqual(state['id'], 'delivery-contract')
        self.assertIsNone(state['physical_ledger'])
        self.assertIsNone(state['semantic_page_runtime'])
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(after, before)

    def test_rejects_changed_fixed_window_and_invalid_caps(self):
        original = deepcopy(self.config)
        for field, value in [('window', {}), ('physical_call_caps', {'model': True}),
                             ('scope', {'cross_session_memory': True}),
                             ('admission', {'concurrent_model_requests': 2})]:
            with self.subTest(field=field):
                self.config = {**original, field: value}
                self.save()
                with self.assertRaises(DeliveryConfigurationError):
                    self.profile()

    def test_rejects_wrong_provider_and_automatic_retry(self):
        original = deepcopy(self.providers)
        for field, value in [('model', 'other-model'), ('retries', 1), ('cache_mode', 'enabled')]:
            with self.subTest(field=field):
                self.providers = deepcopy(original)
                self.providers['luna'][field] = value
                self.save()
                with self.assertRaises(DeliveryConfigurationError):
                    self.profile()

    def test_rejects_output_and_provider_files_outside_run(self):
        self.providers['log_path'] = str(self.root / 'historical.jsonl')
        self.save()
        with self.assertRaises(DeliveryConfigurationError):
            self.profile()
        self.providers['log_path'] = str(self.run / 'calls.jsonl')
        self.provider_path = self.root / 'public-providers.json'
        self.save()
        with self.assertRaises(DeliveryConfigurationError):
            self.profile()

    def test_private_identity_survives_restart_and_invalid_identity_is_not_overwritten(self):
        profile = self.profile()
        path = ensure_private_identity(profile)
        identity = path.read_bytes()
        actors = json.loads(identity)
        self.assertEqual(list(actors.values()), [{'tenant_id': 'delivery-contract', 'principal_id': 'local'}])
        self.assertGreaterEqual(len(next(iter(actors))), 32)
        ensure_private_identity(profile)
        self.assertEqual(path.read_bytes(), identity)
        path.write_text('{}', encoding='utf-8')
        with self.assertRaises(DeliveryConfigurationError):
            ensure_private_identity(profile)
        self.assertEqual(path.read_text('utf-8'), '{}')

    def test_unresolved_request_blocks_preflight_and_factory_without_invoking_application(self):
        ledger = BudgetLedger(self.run / 'physical-calls.sqlite3', self.config['physical_call_caps'])
        reservation = dict.fromkeys(ledger.caps, 0)
        reservation['model'] = 1
        ledger.reserve('synthetic-pending', reservation)
        before = ledger.summary()
        with self.assertRaises(ValueError):
            self.check()

        def unexpected_application(settings):
            self.fail('An unresolved physical request must stop application construction')

        with self.assertRaises(DeliveryConfigurationError):
            create_delivery_app(self.config_path, self.provider_path, self.run, app_factory=unexpected_application)
        self.assertEqual(ledger.summary(), before)

    def test_cli_check_and_help_work_without_services(self):
        help_result = self.cli('--help')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--check', help_result.stdout)
        arguments = ('--check', '--config', str(self.config_path), '--providers', str(self.provider_path),
                     '--run-root', str(self.run))
        result = self.cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'CONFIGURATION_VALID')
        self.config['window'] = {}
        self.save()
        result = self.cli(*arguments)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'CONFIGURATION_INVALID')
        self.assertFalse((self.run / 'identities.json').exists())


if __name__ == '__main__':
    unittest.main()

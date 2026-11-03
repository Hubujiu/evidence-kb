import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.physical_settlement_lint import CAPTURE_CONSUMERS, OWNERS
from scripts.sdk_gateway_lint import ADMISSION, CAPTURE, GATEWAY, MAIN, QUERY, violations
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class SDKGatewayLintContracts(unittest.TestCase):
    def fixture(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'formal').mkdir()
        paths = sorted({ADMISSION, GATEWAY, CAPTURE, MAIN, *(row[0] for row in OWNERS),
                        *(row[0] for row in CAPTURE_CONSUMERS)})
        bindings = []
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((ROOT / path).read_text('utf-8'), 'utf-8')
            binding = {'path': path, 'language': 'python', 'symbol': '<module>'}
            binding['sha256'] = source_digest(root, binding)
            bindings.append(binding)
        registry = {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
            'machines': [{'id': 'synthetic_gateway_lint', 'status': 'unmodeled', 'owners': paths,
                'states': ['synthetic'], 'transitions': ['actual source mutation'],
                'invariants': ['gateway policy survives refreshed source review hashes'],
                'assumptions': ['synthetic lint fixture'], 'exclusions': ['not a product model'],
                'source_bindings': bindings, 'models': [], 'negative_controls': []}]}
        return root, registry

    def check_mutation(self, path, before, after, diagnostic):
        root, registry = self.fixture()
        target = root / path
        original = target.read_text('utf-8')
        self.assertIn(before, original)
        target.write_text(original.replace(before, after, 1), 'utf-8')
        ast.parse(target.read_text('utf-8'))
        for binding in registry['machines'][0]['source_bindings']:
            binding['sha256'] = source_digest(root, binding)
        direct = violations(root)
        self.assertTrue(any(diagnostic in error for error in direct), direct)
        mandatory = registry_violations(root, registry)
        self.assertFalse(any('source review drift' in error for error in mandatory), mandatory)
        self.assertTrue(any('sdk gateway:' in error and diagnostic in error for error in mandatory), mandatory)

    def test_actual_sources_and_unrelated_diagnostics_are_accepted(self):
        self.assertEqual(violations(ROOT), [])
        root, registry = self.fixture()
        self.assertEqual(registry_violations(root, registry), [])
        target = root / GATEWAY
        source = target.read_text('utf-8').replace('                        valid_request = True',
            '                        len(payload)\n                        valid_request = True', 1)
        target.write_text(source, 'utf-8')
        self.assertEqual(violations(root), [])

    def test_locked_failure_status_reset_survives_refreshed_hashes(self):
        for before, after, diagnostic in (
            ('status = 500 if valid_request else 400', 'status = status', 'reset HTTP status'),
            ('                    except Exception as exc:\n                        gateway.blocked = True',
             '                    except Exception as exc:\n                        gateway.blocked = False',
             'reset HTTP status'),
            ('                with gateway.lock:', '                if True:', 'owner lock'),
        ):
            with self.subTest(mutation=after):
                self.check_mutation(GATEWAY, before, after, diagnostic)

    def test_exact_context_and_capture_binding_survive_refreshed_hashes(self):
        mutants = (
            ('context = deepcopy(gateway._context)', 'context = gateway._context', 'deep snapshot'),
            ('{"phase": context, "status": status', '{"phase": gateway.context, "status": status', 'deep snapshot'),
            ('return deepcopy(self._context)', 'return self._context', 'deep-copy getter'),
            ('self._context = deepcopy(value)', 'self._context = value', 'deep-copy getter'),
            ('physical_context = deepcopy(context)', 'physical_context = context', 'physical context'),
            ('physical_id = response.extensions["_kb_physical_request_id"]', 'physical_id = "0" * 32',
             'exact new physical identity'),
            ('or record["context"] != physical_context', 'or False', 'exact new physical identity'),
            ('or capture_unresolved(record)', 'or False', 'exact new physical identity'),
            ('or record["response"] != body', 'or False', 'exact new physical identity'),
            ('if k not in {"retry_of", "retry_reason"}', 'if k not in {"retry_of", "retry_reason", "stage"}',
             'complete logical context'),
        )
        for before, after, diagnostic in mutants:
            with self.subTest(mutation=after):
                self.check_mutation(GATEWAY, before, after, diagnostic)
        self.check_mutation(CAPTURE, "response.extensions['_kb_physical_request_id'] = identity",
                            "response.extensions['_kb_physical_request_id'] = 'old'", 'after settlement')

    def test_gate_and_no_replay_admission_survive_refreshed_hashes(self):
        mutants = (
            ('if gateway.blocked or gateway.closing:', 'if gateway.blocked:', 'blocked/closing'),
            ('if key in gateway.seen and not cached:', 'if False:', 'duplicate guards'),
            ('gateway.seen.add(key)', 'gateway.seen.clear()', 'remembered dispatch'),
            ('if status >= 400:', 'if status > 500:', 'terminal HTTP errors'),
        )
        for before, after, diagnostic in mutants:
            with self.subTest(mutation=after):
                self.check_mutation(GATEWAY, before, after, diagnostic)

    def test_owned_handler_quiescence_survives_refreshed_hashes(self):
        mutants = (
            ('self.server.daemon_threads = False', 'self.server.daemon_threads = True', 'non-daemon'),
            ('self.server.block_on_close = True', 'self.server.block_on_close = False', 'non-daemon'),
            ('            self.server.server_close()\n            self.thread.join()\n            self.client.close()',
             '            self.client.close()\n            self.server.server_close()\n            self.thread.join()',
             'drain handlers'),
            ('            self.thread.join()\n', '', 'drain handlers'),
            ('            with self.lock:\n                self.closing = True',
             '            with self.lock:\n                self.closing = False', 'drain handlers'),
            ('        with self.close_lock:', '        if True:', 'drain handlers'),
        )
        for before, after, diagnostic in mutants:
            with self.subTest(mutation=after):
                self.check_mutation(GATEWAY, before, after, diagnostic)

    def test_recovery_validation_and_retry_lineage_survive_refreshed_hashes(self):
        mutants = (
            ('if capture_unresolved(old):', 'if False:', 'reject unresolved'),
            ('if len(completed) != 1 or completed[0].get("http_status") != 200:',
             'if len(completed) != 1:', 'one replayable completed'),
            ('if len(leaves) != 1:', 'if False:', 'unique complete acyclic lineage'),
            ('if linked != set(group) or current in linked:', 'if linked != set(group):',
             'unique complete acyclic lineage'),
        )
        for before, after, diagnostic in mutants:
            with self.subTest(mutation=after):
                self.check_mutation(GATEWAY, before, after, diagnostic)

    def test_main_constructor_cleanup_and_budget_order_survive_refreshed_hashes(self):
        self.check_mutation(MAIN,
            '        gateway.close()\n        write_json(args.output / "budget-after.json", ledger.cumulative_summary())',
            '        write_json(args.output / "budget-after.json", ledger.cumulative_summary())\n        gateway.close()',
            'drain the owned gateway')
        root, registry = self.fixture()
        target = root / MAIN
        tree = ast.parse(target.read_text('utf-8'))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        attempt = next(node for node in main.body if isinstance(node, ast.Try)
                       and any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                               and item.func.id == 'PageIndexClient' for item in ast.walk(node)))
        constructor = attempt.body.pop(0)
        main.body.insert(main.body.index(attempt), constructor)
        target.write_text(ast.unparse(ast.fix_missing_locations(tree)), 'utf-8')
        for binding in registry['machines'][0]['source_bindings']:
            binding['sha256'] = source_digest(root, binding)
        errors = registry_violations(root, registry)
        self.assertTrue(any('PageIndexClient construction must be protected' in error for error in errors), errors)
        self.assertFalse(any('source review drift' in error for error in errors), errors)

    def test_provider_scope_guard_survives_refreshed_hashes(self):
        self.check_mutation(ADMISSION,
            "'endpoint_sha256': digest(provider.manifest.get('endpoint', provider.base_url))",
            "'endpoint_sha256': 'shared'", 'provider/model/endpoint scope')
        self.check_mutation(ADMISSION, "'model': provider.model", "'model': 'shared'",
                            'provider/model/endpoint scope')
        self.check_mutation(GATEWAY,
            'if any(old.get("provider_scope") != admission.scope_hash for old in self.recovered_records):',
            'if False:', 'semantic provider scope')
        self.check_mutation(GATEWAY, 'or record["provider_scope"] != admission.scope_hash', 'or False',
                            'exact new physical identity')

    def test_query_main_cleanup_survives_refreshed_hashes(self):
        self.check_mutation(QUERY, '        gateway.close()', '        pass',
                            'drain the owned gateway')
        root, registry = self.fixture()
        target = root / QUERY
        tree = ast.parse(target.read_text('utf-8'))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        attempt = next(node for node in main.body if isinstance(node, ast.Try)
                       and any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                               and item.func.id == 'PageIndexClient' for item in ast.walk(node)))
        constructor = attempt.body.pop(0)
        main.body.insert(main.body.index(attempt), constructor)
        target.write_text(ast.unparse(ast.fix_missing_locations(tree)), 'utf-8')
        for binding in registry['machines'][0]['source_bindings']:
            binding['sha256'] = source_digest(root, binding)
        errors = registry_violations(root, registry)
        self.assertTrue(any('PageIndexClient construction must be protected' in error for error in errors), errors)
        self.assertFalse(any('source review drift' in error for error in errors), errors)

    def test_missing_or_malformed_owner_fails_closed(self):
        for content in (None, 'class invalid syntax'):
            with self.subTest(content=content):
                root, _ = self.fixture()
                target = root / GATEWAY
                if content is None:
                    target.unlink()
                else:
                    target.write_text(content, 'utf-8')
                self.assertTrue(any('owner unreadable' in error for error in violations(root)))

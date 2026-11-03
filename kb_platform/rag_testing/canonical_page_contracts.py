from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.core import PlatformError
from kb_platform.google_semantic_provider import GoogleSemanticProvider, write_google_page_capsule
from kb_platform.semantic_page_ingestion import CanonicalPageIngestion
from .budget import BudgetLedger
from .contracts import digest


class SyntheticEncoder:
    provider_id = 'google_gemini'
    dimension = 2
    max_batch_size = 2
    call_resource = 'remote_embedding_calls'
    import_capsule = GoogleSemanticProvider.import_capsule

    def __init__(self, ledger):
        self.ledger = ledger
        self.manifest = {'provider': self.provider_id, 'dimension': self.dimension,
                         'max_tokens': 8192, 'adapter': 'observed-google-semantic/1'}
        self.calls = []
        self.hook = lambda receipt: None
        self.fail = None

    def _unchanged(self):
        pass

    def documents_observed(self, texts, *, request_id, audit_context, authorize):
        if not authorize():
            raise RuntimeError('synthetic authorization denied')
        reserved = {'remote_embedding_calls': 1, 'input_tokens': 20}
        usage = {'remote_embedding_calls': 1, 'input_tokens': len(texts)}
        self.ledger.reserve(request_id, reserved, require_settled=True)
        self.calls.append(request_id)
        if self.fail:
            self.ledger.finish(request_id, status=self.fail, usage=None if self.fail == 'UNKNOWN' else usage)
            raise RuntimeError('synthetic encoder failure')
        self.ledger.finish(request_id, status='COMPLETE', usage=usage)
        vectors = [[1., 0.] for _ in texts]
        receipt = {'request_id': request_id, 'physical_request_id': request_id, 'task': 'document',
                   'status': 'COMPLETE', 'texts': texts,
                   'text_sha256': [hashlib.sha256(text.encode()).hexdigest() for text in texts],
                   'audit_context': deepcopy(audit_context), 'manifest_sha256': digest(self.manifest),
                   'provider_manifest': deepcopy(self.manifest), 'vectors': vectors,
                   'vectors_sha256': digest(vectors), 'usage': usage, 'reserved': reserved,
                   'input_tokens': len(texts)}
        self.hook(receipt)
        return receipt


class SyntheticAuthority:
    def __init__(self):
        self.allowed = True
        self.current = {'scope_id': 'kb', 'manifest_id': 'build', 'authority_revision': 1}
        self.items = {}
        for identity in ('source-a', 'source-b'):
            text = identity + '\nsecond'
            self.items[identity] = {
                'source_identity': {'source_id': identity, 'source_version_id': 'version',
                    'parse_snapshot_id': 'parse', 'source_text_sha256': hashlib.sha256(text.encode()).hexdigest()},
                'original_sha256': hashlib.sha256(identity.encode()).hexdigest(), 'source_text': text,
                'pages': [{'page_index': 0, 'start': 0, 'end': len(identity), 'text': identity},
                          {'page_index': 1, 'start': len(identity) + 1, 'end': len(text), 'text': 'second'}]}

    @contextmanager
    def locked(self, actor, kb_id, *, manage):
        unittest.TestCase().assertTrue(manage and kb_id == 'kb' and actor.tenant_id == 'tenant')
        yield {'state': self.current, 'rows': [{'source_id': identity,
            'policy': {'embedding': True, 'allowed_providers': ['google_gemini']}}
            for identity in self.items], 'authorize': lambda: self.allowed}

    def catalog(self, actor, kb_id, snapshot, rows):
        return deepcopy(self.items)


class CanonicalPageContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ledger = BudgetLedger(self.root / 'budget.db', {'remote_embedding_calls': 10, 'input_tokens': 200})
        self.addCleanup(self.ledger.close)
        self.provider = SyntheticEncoder(self.ledger)
        self.authority = SyntheticAuthority()
        self.output = self.root / 'run'
        self.actor = SimpleNamespace(tenant_id='tenant', principal_id='principal')
        self.sources = {key: {field: value[field] for field in ('source_identity', 'original_sha256')}
                        for key, value in self.authority.items.items()}
        self.runner = CanonicalPageIngestion(self.authority, self.provider, write_google_page_capsule, self.ledger)

    def run_ingestion(self, **overrides):
        options = dict(execute=True, expected_provider_manifest_sha256=digest(self.provider.manifest), batch_size=1)
        options.update(overrides)
        return self.runner.run(self.actor, 'kb', deepcopy(self.authority.current), deepcopy(self.sources),
                               self.output, **options)

    def result(self):
        return json.loads((self.output / 'result.json').read_text('utf-8'))

    def test_two_sources_two_batches_real_capsule_readback_and_durable_accounting(self):
        result = self.run_ingestion()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(self.provider.calls), 4)
        self.assertEqual(len(set(self.provider.calls)), 4)
        self.assertEqual(set(result['capsules']), set(self.sources))
        self.assertEqual(result['document_physical_request_ids'], self.provider.calls)
        self.assertEqual(result['remote_calls'], 4)
        self.assertEqual(result['new_query_embedding_calls'], 0)
        for entry in result['capsules'].values():
            path = Path(entry['path'])
            self.assertEqual(entry['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            value = json.loads(path.read_text('utf-8'))
            self.assertEqual(len(value['pages']), 2)
            self.assertEqual(len(value['provenance']['physical_receipts']), 2)
        self.ledger.close()
        reopened = BudgetLedger(self.root / 'budget.db', {'remote_embedding_calls': 10, 'input_tokens': 200})
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.registered(self.provider.calls), self.provider.calls)
        self.assertTrue(all(reopened.entry(key)['status'] == 'COMPLETE' for key in self.provider.calls))

    def test_existing_output_never_replays_physical_requests(self):
        self.run_ingestion()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaisesRegex(ValueError, 'new output'):
            self.run_ingestion()
        self.assertEqual(len(self.provider.calls), 4)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_explicit_execute_and_clear_ledger_required_before_output_creation(self):
        with self.assertRaisesRegex(ValueError, 'execute=True'):
            self.run_ingestion(execute=False)
        self.ledger.reserve('pending', {'remote_embedding_calls': 1, 'input_tokens': 20})
        with self.assertRaisesRegex(ValueError, 'clear finite ledger'):
            self.run_ingestion()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.provider.calls, [])

    def test_unknown_physical_attempt_keeps_reservation_and_blocks_later_sources(self):
        self.provider.fail = 'UNKNOWN'
        with self.assertRaisesRegex(RuntimeError, 'encoder failure'):
            self.run_ingestion()
        result = self.result()
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['remote_calls'], 1)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(result['completed_capsules'], {})
        self.assertEqual(self.ledger.entry(self.provider.calls[0])['status'], 'UNKNOWN')
        self.assertTrue(result['ledger']['pending_or_unknown'])

    def test_known_failure_keeps_actual_usage_and_never_auto_retries(self):
        self.provider.fail = 'ERROR'
        with self.assertRaises(RuntimeError):
            self.run_ingestion()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.result()['remote_calls'], 1)
        self.assertEqual(self.ledger.entry(self.provider.calls[0])['usage']['input_tokens'], 1)

    def test_receipt_identity_mismatch_keeps_settled_charge_without_capsule(self):
        self.provider.hook = lambda value: value.update(physical_request_id='different-request')
        with self.assertRaises(PlatformError) as error:
            self.run_ingestion()
        self.assertEqual(error.exception.code, 'DOCUMENT_EMBEDDING_RECEIPT_INVALID')
        self.assertEqual(self.result()['completed_capsules'], {})
        self.assertEqual(self.ledger.entry(self.provider.calls[0])['status'], 'COMPLETE')
        self.assertEqual(self.result()['remote_calls'], 1)

    def test_receipt_vector_digest_mismatch_is_rejected_before_capsule_write(self):
        self.provider.hook = lambda value: value.update(vectors_sha256='0' * 64)
        with self.assertRaises(PlatformError):
            self.run_ingestion()
        self.assertEqual(list(self.output.glob('*.capsule.json')), [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_post_encoding_revocation_retains_charge_but_prevents_capsule(self):
        self.provider.hook = lambda value: setattr(self.authority, 'allowed', False)
        with self.assertRaises(PlatformError) as error:
            self.run_ingestion()
        self.assertEqual(error.exception.code, 'SCOPE_CHANGED')
        self.assertEqual(self.result()['remote_calls'], 1)
        self.assertEqual(list(self.output.glob('*.capsule.json')), [])

    def test_partial_source_completion_is_kept_when_second_source_fails(self):
        def fail_later(value):
            if len(self.provider.calls) == 3:
                raise RuntimeError('after third settlement')
        self.provider.hook = fail_later
        with self.assertRaisesRegex(RuntimeError, 'third settlement'):
            self.run_ingestion()
        self.assertEqual(set(self.result()['completed_capsules']), {'source-a'})
        self.assertEqual(self.result()['remote_calls'], 3)
        self.assertFalse((self.output / 'capsules.json').exists())

    def test_capsule_writer_return_path_mismatch_prevents_completion(self):
        writer = self.runner.writer
        def wrong(*args, **kwargs):
            return writer(*args, **kwargs) | {'path': str(self.root / 'different.json')}
        self.runner.writer = wrong
        with self.assertRaises(PlatformError) as error:
            self.run_ingestion()
        self.assertEqual(error.exception.code, 'DOCUMENT_CAPSULE_INVALID')
        self.assertEqual(self.result()['remote_calls'], 2)
        self.assertEqual(self.result()['completed_capsules'], {})

    def test_capsule_import_failure_does_not_report_unread_capsule_complete(self):
        with patch.object(self.provider, 'import_capsule', side_effect=OSError('synthetic readback failure')):
            with self.assertRaises(OSError):
                self.run_ingestion()
        self.assertEqual(self.result()['completed_capsules'], {})
        self.assertEqual(self.result()['remote_calls'], 2)
        self.assertTrue((self.output / 'source-a.capsule.json').exists())

    def test_optional_build_occurs_only_after_all_capsules_and_preserves_failure(self):
        observed = []
        def build(actor, kb_id, capsules, *, expected_state):
            observed.append((deepcopy(capsules), json.loads((self.output / 'capsules.json').read_text())))
            raise RuntimeError('synthetic build failure')
        with self.assertRaisesRegex(RuntimeError, 'build failure'):
            self.run_ingestion(build_service=SimpleNamespace(build=build))
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], observed[0][1])
        self.assertEqual(set(self.result()['completed_capsules']), set(self.sources))
        self.assertEqual(self.result()['remote_calls'], 4)
        self.assertEqual(self.result()['status'], 'ERROR')

    def test_successful_optional_build_result_is_preserved(self):
        built = {'manifest_id': 'new-build', 'status': 'COMPLETE'}
        service = SimpleNamespace(build=lambda *args, **kwargs: built)
        result = self.run_ingestion(build_service=service)
        self.assertEqual(result['formal_build'], built)
        self.assertEqual(result, self.result())

    def test_process_interruption_keeps_settlement_and_refuses_same_output(self):
        def interrupt(value):
            raise KeyboardInterrupt('synthetic process interruption')
        self.provider.hook = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_ingestion()
        self.assertFalse((self.output / 'result.json').exists())
        self.assertEqual(self.ledger.entry(self.provider.calls[0])['status'], 'COMPLETE')
        with self.assertRaisesRegex(ValueError, 'new output'):
            self.run_ingestion()
        self.assertEqual(len(self.provider.calls), 1)

    def test_result_publication_failure_never_returns_complete_or_replays(self):
        import kb_platform.semantic_page_ingestion as ingestion
        original = ingestion._write
        def fail_result(path, value):
            if path.name == 'result.json':
                raise OSError('synthetic disk write failure')
            return original(path, value)
        with patch.object(ingestion, '_write', side_effect=fail_result):
            with self.assertRaises(OSError):
                self.run_ingestion()
        self.assertEqual(len(self.provider.calls), 4)
        self.assertFalse((self.output / 'result.json').exists())
        self.assertTrue((self.output / 'capsules.json').exists())
        self.assertTrue(all(self.ledger.entry(key)['status'] == 'COMPLETE' for key in self.provider.calls))
        with self.assertRaisesRegex(ValueError, 'new output'):
            self.run_ingestion()
        self.assertEqual(len(self.provider.calls), 4)

    def test_stale_source_and_overlapping_pages_fail_before_physical_send(self):
        self.authority.items['source-a']['pages'][1]['start'] = 0
        with self.assertRaises(PlatformError) as error:
            self.run_ingestion()
        self.assertEqual(error.exception.code, 'DOCUMENT_EMBEDDING_STALE')
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.result()['remote_calls'], 0)


class CanonicalPageLintContracts(unittest.TestCase):
    def test_current_owner_has_all_semantic_obligations(self):
        from scripts.canonical_page_checks import canonical_page_violations
        source = (Path(__file__).resolve().parents[1] / 'semantic_page_ingestion.py').read_text('utf-8')
        self.assertEqual(canonical_page_violations(source), [])

    def test_refreshed_source_hash_cannot_hide_semantic_mutations(self):
        import ast
        from scripts.canonical_page_checks import canonical_page_violations
        source = (Path(__file__).resolve().parents[1] / 'semantic_page_ingestion.py').read_text('utf-8')
        mutants = (
            ('result.get("physical_request_id") != request_id', 'False'),
            ('row["status"] != "COMPLETE"', 'False'),
            ('row["usage"] != result.get("usage")', 'False'),
            ('if output.exists():', 'if False:'),
            ('matrix, receipt = self._receipt(result, request_id, texts, context)',
             'matrix, receipt = result["vectors"], {}'),
            ('if not snapshot["authorize"]():', 'if False:'),
            ('imported.manifest.get(\n                        "new_embedding_calls"\n                    ) != 0',
             'False'),
            ('"document_physical_request_ids": _registered(self.ledger, request_ids)',
             '"document_physical_request_ids": []'),
            ('"remote_calls": _remote_calls(self.ledger, request_ids)', '"remote_calls": 0'),
        )
        original_hash = digest(ast.dump(ast.parse(source), include_attributes=False))
        for before, after in mutants:
            with self.subTest(before=before):
                self.assertIn(before, source)
                mutated = source.replace(before, after)
                refreshed_hash = digest(ast.dump(ast.parse(mutated), include_attributes=False))
                self.assertNotEqual(original_hash, refreshed_hash)
                self.assertTrue(canonical_page_violations(mutated))

    def test_mandatory_registry_gate_invokes_owner_check(self):
        from .formal_state_machines import ROOT, registry_violations
        import kb_platform.rag_testing.formal_state_machines as gates
        with patch.object(gates, 'canonical_page_violations', return_value=['canonical sentinel']):
            self.assertIn('canonical sentinel', registry_violations(ROOT))

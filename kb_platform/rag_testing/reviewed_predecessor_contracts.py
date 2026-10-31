from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import httpx

from .budget import BudgetLedger
from .budget_batches import _snapshot, freeze_predecessor
from .provider_capture import MeteredTransport
from .budget_batches import ReviewedPredecessorLedger, freeze_reviewed_predecessor


class ReviewedPredecessorContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        from kb_platform.delivery import RUNTIME_CAPS
        self.caps = dict(RUNTIME_CAPS)
        self.old = BudgetLedger(self.root / 'old' / 'physical-calls.sqlite3', self.caps)
        self.reserved = dict(model=1, input_tokens=100, output_tokens=100,
                             remote_embedding_calls=0, embedding_input_tokens=0)
        self.old.reserve('unknown', self.reserved, category='generation:model')
        self.old.finish('unknown', status='UNKNOWN', diagnostic='ReadTimeout')
        self.record = self.old.path.parent / 'physical-requests' / 'unknown.json'
        self.record.parent.mkdir()
        self.record.write_text(json.dumps(dict(physical_request_id='unknown', status='UNKNOWN',
                                              category='generation:model', reserved=self.reserved)), 'utf-8')
        self.review_path = self.root / 'review.json'
        self.review = dict(schema='physical-model-outcomes-review/1', operator='codex-local-audit',
                           reason='Read timeout; audited both receipts; response unavailable',
                           authorization_reference='User requested continued acceptance',
                           recovery_scope='New bounded batch; preserve old failure and cost association',
                           producers_quiescent=True, recoverable_response_found=False,
                           old_unknown_remains_unknown_and_reserved=True, snapshot=_snapshot(self.old.path),
                           unknown_ids=['unknown'], records={'unknown': sha256(self.record.read_bytes()).hexdigest()})

    def freeze(self):
        self.review_path.write_text(json.dumps(self.review), 'utf-8')
        return freeze_reviewed_predecessor(self.old.path, self.review_path)

    def test_unreviewed_unknown_still_blocks(self):
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PREDECESSOR'):
            freeze_predecessor(self.old.path)

    def test_review_retains_unknown_reservation_and_links_new_physical_request(self):
        before = self.old.path.read_bytes()
        ledger = ReviewedPredecessorLedger(self.root / 'new' / 'physical-calls.sqlite3', self.caps,
                                           predecessors=[self.freeze()])
        transport = MeteredTransport(httpx.MockTransport(lambda request: httpx.Response(200, json={
            'usage': {'prompt_tokens': 3, 'completion_tokens': 4}})), ledger,
            self.root / 'new' / 'physical-requests', category='generation:model')
        with httpx.Client(transport=transport) as client:
            client.post('https://example.test', json={'messages': [], 'max_tokens': 20})
        capture, = (self.root / 'new' / 'physical-requests').glob('*.json')
        record = json.loads(capture.read_text('utf-8'))
        self.assertNotEqual(record['physical_request_id'], 'unknown')
        self.assertEqual(record['reviewed_predecessors'][0]['unknown_physical_ids'], ['unknown'])
        result = ledger.cumulative_summary()
        self.assertEqual(result['status'], 'REVIEWED_UNKNOWN')
        self.assertEqual(result['cumulative']['pending_or_unknown'], 1)
        self.assertEqual(result['admission_pending_or_unknown'], 0)
        self.assertEqual(result['cumulative']['effective_usage_or_reserved']['input_tokens'], 103)
        self.assertEqual(self.old.path.read_bytes(), before)

    def test_missing_authorization_or_unreviewed_producer_rejected(self):
        for key, value in [('authorization_reference', ''), ('producers_quiescent', False),
                           ('recoverable_response_found', True), ('unknown_ids', [])]:
            original = deepcopy(self.review)
            with self.subTest(key=key):
                self.review[key] = value
                with self.assertRaises(ValueError):
                    self.freeze()
            self.review = original

    def test_changed_capture_or_review_blocks_before_send(self):
        frozen = self.freeze()
        ledger = ReviewedPredecessorLedger(self.root / 'new.sqlite3', self.caps, predecessors=[frozen])
        self.record.write_text('{}', 'utf-8')
        with self.assertRaises(ValueError):
            ledger.reserve('next', self.reserved)
        self.assertEqual(ledger.ledger.summary()['physical_requests'], 0)

    def test_new_unknown_is_not_covered_by_old_review(self):
        ledger = ReviewedPredecessorLedger(self.root / 'new.sqlite3', self.caps, predecessors=[self.freeze()])
        ledger.reserve('new-unknown', self.reserved)
        ledger.finish('new-unknown', status='UNKNOWN')
        self.assertEqual(ledger.cumulative_summary()['status'], 'BLOCKED')
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
            ledger.reserve('another', self.reserved)

    def test_delivery_loads_reviewed_batch_but_rejects_changed_review(self):
        from kb_platform.delivery import DeliveryConfigurationError, load_budget_batch

        semantic = BudgetLedger(self.root / 'semantic.sqlite3', self.caps)
        batch = self.root / 'runtime' / 'batch.json'
        batch.parent.mkdir()
        batch.write_text(json.dumps(dict(protocol='delivery-runtime-budget-batch/1',
            id='runtime-reviewed', tenant_id='tenant', caps=self.caps, semantic_predecessor=str(semantic.path),
            predecessors=[self.freeze(), freeze_predecessor(semantic.path)])), 'utf-8')
        profile = SimpleNamespace(root=self.old.path.parent, config={'id': 'tenant'})
        self.assertEqual(load_budget_batch(profile, batch)['id'], 'runtime-reviewed')
        self.review['reason'] = 'different review after registration'
        self.review_path.write_text(json.dumps(self.review), 'utf-8')
        with self.assertRaises(DeliveryConfigurationError):
            load_budget_batch(profile, batch)

    def test_active_old_producer_blocks_successor_even_with_quiescent_attestation(self):
        frozen = self.freeze()
        with self.old.admission():
            with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_BUSY'):
                ReviewedPredecessorLedger(self.root / 'new.sqlite3', self.caps, predecessors=[frozen])
        self.assertFalse((self.root / 'new.sqlite3').exists())
        ledger = ReviewedPredecessorLedger(self.root / 'new.sqlite3', self.caps, predecessors=[frozen])
        self.assertEqual(ledger.summary()['physical_requests'], 0)

    def test_sealed_old_producer_and_reopened_instance_cannot_reserve(self):
        frozen = self.freeze()
        ReviewedPredecessorLedger(self.root / 'new.sqlite3', self.caps, predecessors=[frozen])
        for old in (self.old, BudgetLedger(self.old.path, self.caps)):
            with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_SEALED'):
                old.reserve('late', self.reserved)
        self.assertEqual(self.old.summary()['physical_requests'], 1)

    def test_sibling_batch_rejected_but_descendant_can_continue(self):
        frozen = self.freeze()
        first = ReviewedPredecessorLedger(self.root / 'first.sqlite3', self.caps, predecessors=[frozen])
        with self.assertRaisesRegex(ValueError, 'BUDGET_SUCCESSOR_CONFLICT'):
            ReviewedPredecessorLedger(self.root / 'sibling.sqlite3', self.caps, predecessors=[frozen])
        second = ReviewedPredecessorLedger(self.root / 'second.sqlite3', self.caps,
                                           predecessors=[frozen, freeze_predecessor(first.path)])
        with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_SEALED'):
            first.reserve('stale', self.reserved)
        second.reserve('fresh', self.reserved)
        second.finish('fresh', status='COMPLETE', usage=self.reserved)
        self.assertEqual(second.summary()['physical_requests'], 1)

    def test_failed_validation_does_not_seal_parent(self):
        frozen = self.freeze()
        self.record.write_text('{}', 'utf-8')
        with self.assertRaises(ValueError):
            ReviewedPredecessorLedger(self.root / 'bad.sqlite3', self.caps, predecessors=[frozen])
        with self.old.admission():
            pass

    def test_exclusion_spans_actual_send_and_releases_after_response(self):
        from kb_platform.provider_admission import CapturePreflightError
        from kb_platform.embedding_jobs import classify_provider_failure, EmbeddingPreflightError
        old = BudgetLedger(self.root / 'settled.sqlite3', self.caps)
        frozen = freeze_predecessor(old.path)
        reopened = BudgetLedger(old.path, self.caps)
        sent = []

        def respond(request):
            sent.append(request)
            with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_BUSY'):
                reopened.reserve('concurrent', self.reserved)
            with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_BUSY'):
                ReviewedPredecessorLedger(self.root / 'racing.sqlite3', self.caps, predecessors=[frozen])
            return httpx.Response(200, json={'usage': {'prompt_tokens': 3, 'completion_tokens': 4}})

        transport = MeteredTransport(httpx.MockTransport(respond), old, self.root / 'captures', category='model')
        with httpx.Client(transport=transport) as client:
            client.post('https://example.test', json={'messages': [], 'max_tokens': 20})
            ReviewedPredecessorLedger(self.root / 'next.sqlite3', self.caps,
                                       predecessors=[freeze_predecessor(old.path)])
            with self.assertRaisesRegex(CapturePreflightError, 'BUDGET_PRODUCER_SEALED') as caught:
                client.post('https://example.test', json={'messages': [], 'max_tokens': 20})
        classified = classify_provider_failure({}, None, caught.exception)
        self.assertIsInstance(classified, EmbeddingPreflightError)
        self.assertEqual(classified.code, 'EMBEDDING_LEDGER_BLOCKED')
        self.assertEqual(len(sent), 1)
        self.assertEqual(old.summary()['physical_requests'], 1)

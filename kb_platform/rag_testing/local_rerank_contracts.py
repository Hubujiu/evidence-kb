"""Deterministic adapter/ledger contracts; synthetic scores are not model quality."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .budget import BudgetLedger
from .local_rerank import LocalRanker


def candidate(text):
    return SimpleNamespace(text=text, model_dump=lambda: {'text': text})


class LocalRerankContracts(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.ledger = BudgetLedger(self.root / 'ledger.sqlite3',
                                   {'local_batches': 2, 'pairs': 5, 'remote_calls': 0})
        self.request = SimpleNamespace(question='Question?', case_id='case-1',
                                       model_dump=lambda: {'question': 'Question?', 'case_id': 'case-1'})
        self.items = [candidate(text) for text in ['original A', ' B ', 'C', 'D', 'E']]
        self.ranker = object.__new__(LocalRanker)
        self.ranker.name = 'bge-base'
        self.ranker.ledger, self.ranker.output = self.ledger, self.raw
        self.ranker.manifest = {'max_length': 512, 'revision': 'fixed', 'engine': 'CrossEncoder'}
        self.ranker.torch = SimpleNamespace(inference_mode=nullcontext)
        self.ranker.tokenizer = Mock(side_effect=lambda pairs, **kwargs: {'input_ids': [[1, 2, 3] for _ in pairs]})
        self.ranker.encoder = SimpleNamespace(predict=Mock(side_effect=self.predict))

    def predict(self, pairs, **kwargs):
        # The application must reserve before entering library inference.
        with closing(sqlite3.connect(self.ledger.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calls WHERE status='STARTED'").fetchone()[0], 1)
        return SimpleNamespace(tolist=lambda: [0.5] * len(pairs))

    def statuses(self):
        with closing(sqlite3.connect(self.ledger.path)) as db:
            return db.execute('SELECT status FROM calls ORDER BY rowid').fetchall()

    def test_batch_reservations_scores_order_and_original_pairs(self):
        order, detail = self.ranker.rank(self.request, self.items)
        self.assertEqual(order, [0, 1, 2, 3, 4])
        self.assertEqual(detail['scores'], [0.5] * 5)
        self.assertEqual(detail['tokens'], 15)
        self.assertEqual([r['offset'] for r in detail['batches']], [0, 4])
        self.assertEqual([len(c.args[0]) for c in self.ranker.encoder.predict.call_args_list], [4, 1])
        self.assertEqual(self.ranker.encoder.predict.call_args_list[0].args[0][1], ('Question?', ' B '))
        for call in self.ranker.encoder.predict.call_args_list:
            self.assertEqual(call.kwargs['processing_kwargs'], {'text': {'truncation': False}})
            self.assertEqual(call.kwargs['prompt'], '')
            self.assertEqual(call.kwargs['batch_size'], 4)
        self.assertEqual(self.statuses(), [('COMPLETE',), ('COMPLETE',)])
        self.assertEqual(self.ledger.summary()['effective_usage_or_reserved'],
                         {'local_batches': 2, 'pairs': 5, 'remote_calls': 0})
        records = [json.loads(p.read_text('utf-8')) for p in self.raw.glob('*.json')]
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(len(r['scores']) for r in records), 5)

    def test_overlength_rejected_before_any_reservation_or_inference(self):
        self.ranker.tokenizer.return_value = {'input_ids': [[1] * 513]}
        self.ranker.tokenizer.side_effect = None
        with self.assertRaisesRegex(ValueError, 'no silent truncation'):
            self.ranker.rank(self.request, self.items[:1])
        self.ranker.encoder.predict.assert_not_called()
        self.assertEqual(self.statuses(), [])

    def test_exact_length_limit_is_admitted(self):
        self.ranker.tokenizer.side_effect = None
        self.ranker.tokenizer.return_value = {'input_ids': [[1] * 512]}
        _, detail = self.ranker.rank(self.request, self.items[:1])
        self.assertEqual(detail['tokens'], 512)
        self.assertEqual(self.statuses(), [('COMPLETE',)])

    def test_budget_refusal_does_not_call_library(self):
        self.ranker.ledger = BudgetLedger(self.root / 'empty-budget.sqlite3',
                                          {'local_batches': 0, 'pairs': 0, 'remote_calls': 0})
        with self.assertRaisesRegex(ValueError, 'BUDGET_INSUFFICIENT'):
            self.ranker.rank(self.request, self.items)
        self.ranker.encoder.predict.assert_not_called()
        self.assertEqual(self.statuses(), [])

    def test_completed_batch_is_not_replayed(self):
        self.ranker.rank(self.request, self.items[:1])
        self.ranker.encoder.predict.reset_mock()
        with self.assertRaisesRegex(ValueError, 'already registered'):
            self.ranker.rank(self.request, self.items[:1])
        self.ranker.encoder.predict.assert_not_called()

    def test_inference_failure_keeps_reservation_and_does_not_retry(self):
        self.ranker.encoder.predict.side_effect = RuntimeError('device failure')
        with self.assertRaisesRegex(RuntimeError, 'device failure'):
            self.ranker.rank(self.request, self.items)
        self.assertEqual(self.statuses(), [('ERROR',)])
        self.assertEqual(self.ranker.encoder.predict.call_count, 1)
        self.assertEqual(self.ledger.summary()['effective_usage_or_reserved']['pairs'], 4)
        with self.assertRaisesRegex(ValueError, 'already registered'):
            self.ranker.rank(self.request, self.items)
        self.assertEqual(self.ranker.encoder.predict.call_count, 1)

    def test_interruption_retains_unresolved_reservation(self):
        self.ranker.encoder.predict.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.ranker.rank(self.request, self.items)
        self.assertEqual(self.statuses(), [('STARTED',)])
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
            self.ranker.rank(self.request, self.items)
        self.assertEqual(self.ranker.encoder.predict.call_count, 1)
        self.assertEqual(self.ledger.summary()['pending_or_unknown'], 1)

    def test_empty_candidates_have_no_inference(self):
        order, detail = self.ranker.rank(self.request, [])
        self.assertEqual(order, [])
        self.assertEqual(detail['batches'], [])
        self.ranker.encoder.predict.assert_not_called()

    def test_invalid_scores_are_not_delivered(self):
        for values in [[], [float('nan')], [float('inf')], [True], [-0.1], [1.1], [[0.5]], [0.1, 0.2]]:
            with self.subTest(values=values):
                self.ranker.manifest['test_case'] = repr(values)
                self.ranker.ledger = BudgetLedger(self.root / f'ledger-{len(list(self.root.glob("ledger-*")))}.sqlite3',
                                                   {'local_batches': 1, 'pairs': 1, 'remote_calls': 0})
                self.ranker.encoder.predict.side_effect = None
                self.ranker.encoder.predict.return_value = SimpleNamespace(tolist=lambda: values)
                with self.assertRaisesRegex(ValueError, 'Invalid model scores'):
                    self.ranker.rank(self.request, self.items[:1])
                self.assertEqual(self.ranker.ledger.summary()['unknown_usage_requests'], 1)
        self.assertEqual(list(self.raw.glob('*.json')), [])

    def test_library_scores_remain_bound_to_input_positions(self):
        self.ranker.encoder.predict.side_effect = None
        self.ranker.encoder.predict.return_value = SimpleNamespace(tolist=lambda: [0.2, 0.9, 0.2])
        order, _ = self.ranker.rank(self.request, self.items[:3])
        self.assertEqual(order, [1, 0, 2])

    def test_bge_loads_fixed_local_cross_encoder_without_legacy_classifier(self):
        encoder = Mock(tokenizer=Mock())
        factory = Mock(return_value=encoder)
        torch = SimpleNamespace(set_num_threads=Mock(), float32='float32', float16='float16', __version__='test')
        transformers = SimpleNamespace(AutoModelForCausalLM=Mock(), AutoTokenizer=Mock(), __version__='test')
        package = SimpleNamespace(CrossEncoder=factory, __version__='5.7.0')
        with patch.dict('sys.modules', torch=torch, transformers=transformers, sentence_transformers=package):
            ranker = LocalRanker('bge-base', self.ledger, self.raw)
        args, kwargs = factory.call_args
        self.assertEqual(args, ('BAAI/bge-reranker-base',))
        self.assertEqual(kwargs['revision'], '2cfc18c9415c912f9d8155881c133215df768a70')
        self.assertTrue(kwargs['local_files_only'])
        self.assertFalse(kwargs['trust_remote_code'])
        self.assertFalse(kwargs['token'])
        self.assertEqual(kwargs['max_length'], 512)
        self.assertEqual(kwargs['model_kwargs'], {'torch_dtype': 'float32'})
        self.assertEqual(ranker.manifest['sentence_transformers_version'], '5.7.0')
        transformers.AutoModelForCausalLM.from_pretrained.assert_not_called()


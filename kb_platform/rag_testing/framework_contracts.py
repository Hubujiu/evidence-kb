"""Offline behavioral contracts for cache, physical-call budgets and frozen runs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from .budget import BudgetLedger
from .cache import StageCache, StageKey
from .contracts import AdapterInput, Case, Plan, Source, Suite, Trace, digest, text_hash
from .runner import run


class StageCacheContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = StageCache(self.root)
        self.key = StageKey(stage='assembly', implementation='v1', upstream=('question-a', 'history-a'),
            config_hash='config', corpus_hash='corpus', index_snapshot='index', principal='alice',
            acl_revision='acl-1', model_revision='model', sample_id='sample-1')
        self.payload = {'text': 'authorized evidence'}
        self.cache.put(self.key, self.payload)

    def test_exact_dependencies_hit_and_each_changed_dependency_misses(self):
        self.assertEqual(self.cache.get(self.key, authorize=lambda: True), self.payload)
        changes = [{'upstream': ('history-a', 'question-a')}, {'upstream': ('question-b', 'history-a')},
                   {'upstream': ('question-a', 'history-b')}]
        changes.extend({field: 'changed'} for field in type(self.key).model_fields if field != 'upstream')
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.cache.get(self.key.model_copy(update=change), authorize=lambda: True))

    def test_every_read_rechecks_current_authorization(self):
        authorize = Mock(side_effect=[True, False])
        self.assertEqual(self.cache.get(self.key, authorize=authorize), self.payload)
        with self.assertRaises(PermissionError):
            self.cache.get(self.key, authorize=authorize)
        self.assertEqual(authorize.call_count, 2)

    def test_corrupt_cached_content_is_rejected(self):
        (self.root / 'objects' / (digest(self.payload) + '.json')).write_text('{"text":"forged"}', 'utf-8')
        with self.assertRaises(ValueError):
            self.cache.get(self.key, authorize=lambda: True)

    def test_cold_and_live_under_test_bypass_cached_output(self):
        for mode, under_test, expected in (
            ('cold', False, None), ('cold', True, None), ('live', True, None),
            ('live', False, self.payload), ('replay', False, self.payload),
        ):
            with self.subTest(mode=mode, under_test=under_test):
                self.assertEqual(self.cache.get(self.key, authorize=lambda: True,
                                                mode=mode, under_test=under_test), expected)

    def test_existing_identity_cannot_be_rebound_to_changed_output(self):
        with self.assertRaises(ValueError):
            self.cache.put(self.key, {'text': 'different sample'})
        self.assertEqual(self.cache.get(self.key, authorize=lambda: True), self.payload)


class BudgetLedgerContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'ledger.sqlite3'
        self.caps = {'calls': 3, 'tokens': 100}
        self.ledger = BudgetLedger(self.path, self.caps)

    def test_unknown_and_partial_usage_keep_unreported_reservations_after_reopen(self):
        self.ledger.reserve('unknown', {'calls': 1, 'tokens': 30})
        self.ledger.finish('unknown', status='UNKNOWN')
        self.ledger.reserve('partial', {'calls': 1, 'tokens': 40})
        self.ledger.finish('partial', status='ERROR', usage={'calls': 1})
        summary = BudgetLedger(self.path, self.caps).summary()
        self.assertEqual(summary['effective_usage_or_reserved'], {'calls': 2, 'tokens': 70})
        self.assertEqual(summary['unknown_usage_requests'], 2)
        self.assertEqual(summary['pending_or_unknown'], 1)
        with self.assertRaises(ValueError):
            self.ledger.reserve('new', {'calls': 1, 'tokens': 10}, require_settled=True)
        with self.assertRaises(ValueError):
            self.ledger.reserve('too-expensive', {'calls': 1, 'tokens': 31})
        self.assertEqual(self.ledger.summary()['physical_requests'], 2)

    def test_duplicate_physical_identity_is_rejected_in_every_state(self):
        for status in ('STARTED', 'COMPLETE', 'ERROR', 'UNKNOWN'):
            with self.subTest(status=status):
                ledger = BudgetLedger(self.path.with_name(status + '.sqlite3'), self.caps)
                ledger.reserve('one', {'calls': 1, 'tokens': 20})
                if status != 'STARTED':
                    ledger.finish('one', status=status, usage={'calls': 1, 'tokens': 12})
                with self.assertRaises(ValueError):
                    ledger.reserve('one', {'calls': 1, 'tokens': 20})
                self.assertEqual(ledger.summary()['physical_requests'], 1)

    def test_frozen_caps_cannot_be_changed_on_reopen(self):
        with self.assertRaises(ValueError):
            BudgetLedger(self.path, {'calls': 4, 'tokens': 100})
        self.assertEqual(BudgetLedger(self.path, self.caps).summary()['physical_requests'], 0)

    def test_actual_overrun_is_retained_and_blocks_new_reservations(self):
        self.ledger.reserve('one', {'calls': 1, 'tokens': 20})
        self.ledger.finish('one', status='COMPLETE', usage={'calls': 1, 'tokens': 101})
        self.assertTrue(self.ledger.summary()['over_budget'])
        self.assertEqual(self.ledger.summary()['effective_usage_or_reserved']['tokens'], 101)
        with self.assertRaises(ValueError):
            self.ledger.reserve('two', {'calls': 1, 'tokens': 1})


class FrozenRunnerContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / 'run'
        source = Source(id='allowed', kb='kb', version='1', text='public', sha256=text_hash('public'))
        private = Source(id='private', kb='kb', version='1', text='private', sha256=text_hash('private'))
        cases = tuple(Case(input=AdapterInput(case_id=identity, question='Question?', kb='kb',
            principal='alice', acl_revision='1', allowed_sources=('allowed',), as_of='2026-10-28',
            context_budget=1000), family=identity, expected_behavior='abstain',
            qrels={'allowed': 1}, forbidden_text=('gold-only-marker',)) for identity in ('one', 'two'))
        self.suite = Suite(id='runner-control', label_provenance='synthetic_engineering',
                           sources=(source, private), evidence=(), cases=cases)
        self.plan = Plan(suite_hash=digest(self.suite))

    def test_changed_suite_is_rejected_before_creating_output(self):
        with self.assertRaises(ValueError):
            run(self.suite, self.plan.model_copy(update={'suite_hash': 'wrong'}), self.output, traces=[])
        self.assertFalse(self.output.exists())

    def test_missing_traces_keep_frozen_denominator_and_not_run_outcomes(self):
        report = run(self.suite, self.plan, self.output, traces=[])
        self.assertEqual(report['total'], 2)
        self.assertEqual(report['counts']['NOT_RUN'], 2)
        self.assertEqual({row['case_id'] for row in report['rows']}, {'one', 'two'})
        self.assertEqual(json.loads((self.output / 'report.json').read_text('utf-8'))['total'], 2)

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / 'existing.txt'
        marker.write_text('preserve', 'utf-8')
        with self.assertRaises(FileExistsError):
            run(self.suite, self.plan, self.output, traces=[])
        self.assertEqual(marker.read_text('utf-8'), 'preserve')
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_adapter_only_receives_inputs_and_current_authorized_sources(self):
        calls = []

        class RecordingAdapter:
            implementation = 'offline-control'

            def run(self, request, sources, corpus):
                calls.append((request, sources))
                return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
                             principal=request.principal, acl_revision=request.acl_revision, status='NOT_RUN')

        run(self.suite, self.plan.model_copy(update={'mode': 'live'}), self.output, adapter=RecordingAdapter())
        self.assertEqual(len(calls), 2)
        for (request, sources), case in zip(calls, self.suite.cases):
            self.assertEqual(request, case.input)
            self.assertIsNot(request, case.input)
            self.assertEqual([source.id for source in sources], ['allowed'])
            self.assertNotIn('gold-only-marker', request.model_dump_json())
            self.assertNotIn('qrels', request.model_dump())
            self.assertNotIn('any_of', request.model_dump())
            self.assertIsNot(sources[0], self.suite.sources[0])


if __name__ == '__main__':
    unittest.main()

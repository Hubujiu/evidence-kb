from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from kb_platform.persistence_local import local_database
from kb_platform.persistence_migration import import_ledger, inspect_ledger_import
from . import budget_batches
from .budget import BudgetLedger


class BatchSealingTransitionContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.caps = {'model': 2}

    def ledger(self, name, identity):
        ledger = BudgetLedger(self.root / (name + '.sqlite'), self.caps)
        self.addCleanup(ledger.close)
        ledger.reserve(identity, {'model': 1})
        ledger.finish(identity, status='COMPLETE', usage={'model': 1})
        return ledger

    def seals(self):
        result = []
        for name in ('a', 'b'):
            with closing(sqlite3.connect(str(self.root / (name + '.sqlite')) + '.producer.sqlite3')) as db:
                row = db.execute('SELECT successor FROM producer WHERE id=1').fetchone()
                result.append(None if row is None else row[0])
        return result

    def test_real_partial_guard_commit_recovers_same_child_without_resetting_independent_cost(self):
        first, second = self.ledger('a', 'request-a'), self.ledger('b', 'request-b')
        predecessors = [budget_batches.freeze_predecessor(ledger.path) for ledger in (first, second)]
        originals = [ledger.path.read_bytes() for ledger in (first, second)]
        child = self.root / 'child.sqlite'
        case = self

        class TrackedLedger(BudgetLedger):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                case.addCleanup(self.close)

        with closing(sqlite3.connect(str(first.path) + '.producer.sqlite3', timeout=0)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT successor FROM producer WHERE id=1').fetchall()
            try:
                with patch.object(budget_batches, 'BudgetLedger', TrackedLedger):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        budget_batches.LinkedBudgetLedger(child, self.caps, predecessors=predecessors)
                self.assertEqual(caught.exception.sqlite_errorname, 'SQLITE_BUSY')
            finally:
                reader.rollback()
        self.assertEqual(self.seals(), [None, str(child)])
        with first.admission():
            pass
        with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_SEALED'):
            second.reserve('late-parent', {'model': 1})
        with self.assertRaisesRegex(ValueError, 'BUDGET_SUCCESSOR_CONFLICT'):
            budget_batches.LinkedBudgetLedger(self.root / 'sibling.sqlite', self.caps, predecessors=predecessors)
        with patch.object(budget_batches, 'BudgetLedger', TrackedLedger):
            resumed = budget_batches.LinkedBudgetLedger(child, self.caps, predecessors=predecessors)
        self.assertEqual(self.seals(), [str(child), str(child)])
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_PHYSICAL_REQUEST_ID'):
            resumed.reserve('request-a', {'model': 1})
        resumed.reserve('child-request', {'model': 1})
        resumed.finish('child-request', status='COMPLETE', usage={'model': 1})
        with self.assertRaisesRegex(ValueError, 'already registered'):
            resumed.reserve('child-request', {'model': 1})
        report = resumed.cumulative_summary()
        self.assertEqual(report['cumulative']['physical_requests'], 3)
        self.assertEqual(report['cumulative']['effective_usage_or_reserved'], {'model': 3})
        self.assertFalse(report['cumulative']['over_budget'])
        self.assertTrue(report['caps_are_independent_not_a_merged_spendable_pool'])
        self.assertEqual([ledger.path.read_bytes() for ledger in (first, second)], originals)

    def test_duplicate_parent_request_is_rejected_before_any_parent_is_sealed(self):
        first, second = self.ledger('a', 'same-physical-id'), self.ledger('b', 'same-physical-id')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_PHYSICAL_REQUEST_ID'):
            budget_batches.LinkedBudgetLedger(self.root / 'child.sqlite', self.caps,
                predecessors=[budget_batches.freeze_predecessor(ledger.path) for ledger in (first, second)])
        self.assertEqual(self.seals(), [None, None])
        self.assertFalse((self.root / 'child.sqlite').exists())


class LedgerImportTransitionContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.caps = {'calls': 4, 'tokens': 200}
        self.source = self.root / 'legacy.sqlite'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE config(id INTEGER PRIMARY KEY,caps TEXT)')
            db.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,status TEXT,reserved TEXT,usage TEXT,category TEXT,diagnostic TEXT)')
            db.execute('INSERT INTO config VALUES(1,?)', (json.dumps(self.caps),))
            for identity, status, tokens, usage in (
                    ('a-complete', 'COMPLETE', 20, {'calls': 1, 'tokens': 10}),
                    ('b-unknown', 'UNKNOWN', 71, None)):
                db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)', (identity, status,
                    json.dumps({'calls': 1, 'tokens': tokens}), None if usage is None else json.dumps(usage),
                    'synthetic', 'controlled history'))
        self.original = self.source.read_bytes()
        self.frozen = inspect_ledger_import(self.source)
        self.target = self.root / 'target.sqlite'

    def bind(self, target=None):
        database, owner = local_database(target or self.target, owner='physical-budget')
        self.addCleanup(owner.close)
        return database

    def import_same_target(self, target=None):
        return import_ledger(self.source, self.bind(target), expected_file_sha256=self.frozen['file_sha256'],
                             expected_snapshot_sha256=self.frozen['snapshot_sha256'])

    def assert_source_pinned_and_unchanged(self, identity):
        with closing(sqlite3.connect(str(self.source) + '.producer.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT successor FROM producer WHERE id=1').fetchone(),
                             ('dataset:' + identity,))
        self.assertEqual(self.source.read_bytes(), self.original)
        sealed = BudgetLedger(self.source, self.caps)
        self.addCleanup(sealed.close)
        self.assertEqual(sealed.entry('b-unknown')['status'], 'UNKNOWN')
        self.assertEqual(sealed.summary()['effective_usage_or_reserved'], {'calls': 2, 'tokens': 81})
        with self.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_SEALED'):
            sealed.reserve('forbidden-late', {'calls': 1, 'tokens': 1})

    def test_real_insert_fault_rolls_back_rows_and_marker_then_same_dataset_fresh_binding_recovers(self):
        database = self.bind()
        BudgetLedger(caps=self.caps, database=database)
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("CREATE TRIGGER synthetic_import_failure BEFORE INSERT ON calls "
                       "WHEN NEW.id='b-unknown' BEGIN SELECT RAISE(ABORT,'SYNTHETIC_IMPORT_FAULT'); END")
        with self.assertRaises(IntegrityError):
            self.import_same_target()
        self.assert_source_pinned_and_unchanged(database.identity.dataset_id)
        with closing(sqlite3.connect(self.target)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM ledger_imports').fetchone()[0], 0)
            db.execute('DROP TRIGGER synthetic_import_failure')
        resumed = self.import_same_target()
        before = resumed.summary()
        replayed = self.import_same_target()
        self.assertEqual(replayed.summary(), before)
        self.assertEqual(before['physical_requests'], 2)
        self.assertEqual(before['effective_usage_or_reserved'], {'calls': 2, 'tokens': 81})
        self.assertEqual(before['pending_or_unknown'], 1)
        with closing(sqlite3.connect(self.target)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ledger_imports').fetchone()[0], 1)
        self.assert_source_pinned_and_unchanged(database.identity.dataset_id)

    def test_nonempty_destination_remains_pinned_without_deleting_source_or_destination(self):
        database = self.bind()
        existing = BudgetLedger(caps=self.caps, database=database)
        existing.reserve('preexisting', {'calls': 1, 'tokens': 10})
        existing.finish('preexisting', status='COMPLETE', usage={'calls': 1, 'tokens': 4})
        before = existing.summary()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'LEDGER_IMPORT_DESTINATION_NOT_EMPTY'):
                self.import_same_target()
            self.assertEqual(existing.summary(), before)
            self.assert_source_pinned_and_unchanged(database.identity.dataset_id)
        with self.assertRaisesRegex(ValueError, 'LEDGER_IMPORT_DESTINATION_CHANGED'):
            self.import_same_target(self.root / 'other.sqlite')
        self.assertEqual(existing.summary(), before)
        with closing(sqlite3.connect(self.target)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ledger_imports').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()

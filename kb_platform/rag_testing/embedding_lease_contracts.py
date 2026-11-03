from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import event, select, update

from kb_platform.database_providers import SQLiteProvider
from kb_platform.embedding_jobs import EmbeddingJobError, EmbeddingJobs, EmbeddingPreflightError, digest
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import EMBEDDING_ATTEMPTS, EMBEDDING_CIRCUITS, EMBEDDING_JOBS
from kb_platform.knowledge_storage import KnowledgeRepository, KnowledgeTransaction
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_sdk import ExistingDataset, SpaceKey
from .knowledge_persistence_contracts import LocalEmbeddingProvider


class EmbeddingLeaseContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        provider = SQLiteProvider(Path(temporary.name) / 'database')
        storage = SqlStorageProvider(provider)
        self.addCleanup(storage.close)
        space = SpaceKey('lease-contract', 'knowledge', 'clock')
        identity = storage.initialize('structured', space)
        database = next(item for item in storage.exports() if item.name == 'structured')\
            .open_space(space, ExistingDataset(identity.dataset_id))
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock_database = sqlite3.connect(':memory:')
        self.addCleanup(clock_database.close)

        def strftime(*arguments):
            values = tuple(self.clock.isoformat(' ') if value == 'now' else value for value in arguments)
            return clock_database.execute('SELECT strftime(' + ','.join('?' for _ in values) + ')', values).fetchone()[0]

        def freeze(connection, record):
            connection.create_function('strftime', -1, strftime)

        event.listen(provider.open_space(space).runtime_engine, 'connect', freeze)
        self.repository = KnowledgeRepository(database=database)
        self.jobs = EmbeddingJobs(self.repository)
        self.provider = LocalEmbeddingProvider()
        self.scope, self.units, self.audit = ('tenant', 'kb', 'build'), [{'text': 'synthetic'}], {'acl': 1}

    def _claim(self):
        _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
        return self.jobs._claim(*self.scope, 0, dependency), dependency

    def _rows(self):
        with self.jobs._tx(*self.scope[:2]) as transaction:
            return tuple(transaction._transaction.execute(select(table).order_by(*table.primary_key.columns)).fetchall()
                for table in (EMBEDDING_JOBS, EMBEDDING_ATTEMPTS, EMBEDDING_CIRCUITS))

    def _error(self, code, function, *args, **kwargs):
        with self.assertRaises(EmbeddingJobError) as rejected:
            function(*args, **kwargs)
        self.assertEqual(rejected.exception.code, code)
        return rejected.exception

    def _observation(self, row, *, unsent=False):
        vectors = [[1., 0.]]
        status = 'ERROR' if unsent else 'COMPLETE'
        usage = {'calls': 0 if unsent else 1}
        record = dict(status=status, task='document', request_id=row['request_id'],
            physical_request_id=row['request_id'], texts=['synthetic'], provider_manifest=self.provider.manifest,
            audit_context=self.audit, usage=usage, reserved={'calls': 1},
            physical_embedding_attempts=0 if unsent else 1, vectors=vectors, vectors_sha256=digest(vectors))
        return dict(record=record, physical_status=status, physical_usage=usage)

    def test_sqlite_database_clock_is_frozen_for_clock_and_deadline_expressions(self):
        row, _ = self._claim()
        with self.jobs._tx(*self.scope[:2]) as transaction:
            self.assertEqual(transaction.execute(K.JOB_CLOCK).fetchone()['db_now'], self.clock)
        self.assertEqual(row['lease_until'], self.clock + timedelta(seconds=self.jobs.lease_seconds))

    def test_claim_before_equal_and_after_deadline(self):
        for offset, expected in ((-1, 'EMBEDDING_RUNNING'), (0, 'EMBEDDING_UNKNOWN'), (1, 'EMBEDDING_UNKNOWN')):
            with self.subTest(offset=offset):
                self.scope = ('tenant', 'kb', 'claim-' + str(offset))
                row, dependency = self._claim()
                self.clock = row['lease_until'] + timedelta(milliseconds=offset)
                self._error(expected, self.jobs._claim, *self.scope, 0, dependency)
                current = self.jobs.status(*self.scope)[0]
                self.assertEqual(current['status'], 'RUNNING' if offset < 0 else 'UNKNOWN')
                self.assertEqual((current['attempt'], current['request_id']), (1, row['request_id']))
                attempts = [item for item in self._rows()[1] if item['request_id'] == row['request_id']]
                self.assertEqual(attempts[0]['status'], current['status'])

    def test_finish_before_equal_and_after_deadline(self):
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset):
                self.scope = ('tenant', 'kb', 'finish-' + str(offset))
                row, _ = self._claim()
                self.clock = row['lease_until'] + timedelta(milliseconds=offset)
                if offset > 0:
                    before = self._rows()
                    self._error('EMBEDDING_LEASE_LOST', self.jobs._finish, row, vectors=[[1., 0.]])
                    self.assertEqual(self._rows(), before)
                else:
                    self.jobs._finish(row, vectors=[[1., 0.]])
                    self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'COMPLETE')

    def test_reconcile_strict_expiry_and_both_equality_serializations(self):
        row, dependency = self._claim()
        self.clock = row['lease_until']
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'RUNNING')
        self.jobs._finish(row, vectors=[[1., 0.]])
        self.assertEqual(self.jobs._claim(*self.scope, 0, dependency)['status'], 'COMPLETE')
        self.scope = ('tenant', 'kb', 'claim-first')
        row, dependency = self._claim()
        self.clock = row['lease_until']
        self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
        self._error('EMBEDDING_LEASE_LOST', self.jobs._finish, row, vectors=[[1., 0.]])
        self.provider.recover_embedding = lambda request: self._observation(row)
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'COMPLETE')
        self.scope = ('tenant', 'kb', 'reconcile-after')
        row, _ = self._claim()
        self.clock = row['lease_until'] + timedelta(milliseconds=1)
        self.provider.recover_embedding = lambda request: None
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')

    def test_known_unsent_refund_at_expired_and_unknown_states_is_exactly_once(self):
        for expire in (False, True):
            with self.subTest(expire=expire):
                self.scope = ('tenant', 'kb', 'unsent-' + str(expire))
                row, dependency = self._claim()
                self.clock = row['lease_until'] + timedelta(milliseconds=1)
                if expire:
                    self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
                error = RuntimeError('synthetic unsent')
                error.model_sent = False
                self._error('EMBEDDING_RETRY', self.jobs._finish, row, error=error)
                current = self.jobs.status(*self.scope)[0]
                self.assertEqual((current['status'], current['attempt']), ('RETRY', 0))
                before = self._rows()
                self._error('EMBEDDING_LEASE_LOST', self.jobs._finish, row, error=error)
                self.assertEqual(self._rows(), before)
                successor = self.jobs._claim(*self.scope, 0, dependency)
                self.assertNotEqual(successor['request_id'], row['request_id'])
                self.assertEqual(successor['attempt'], 1)
                before = self._rows()
                self._error('EMBEDDING_LEASE_LOST', self.jobs._finish, row, error=error, recovered=True)
                self.assertEqual(self._rows(), before)

    def test_recovery_snapshot_cannot_complete_or_refund_successor(self):
        for unsent in (False, True):
            with self.subTest(unsent=unsent):
                self.scope = ('tenant', 'kb', 'stale-' + str(unsent))
                row, dependency = self._claim()
                self.clock = row['lease_until']
                self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
                observation = self._observation(row, unsent=unsent)
                successor, persisted = [], []

                def recover(request):
                    if not successor:
                        error = RuntimeError('synthetic zero-send')
                        error.model_sent = False
                        self._error('EMBEDDING_RETRY', self.jobs._finish, row, error=error, recovered=True)
                        successor.append(self.jobs._claim(*self.scope, 0, dependency))
                        self.clock = successor[0]['lease_until']
                        self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
                        persisted.append(self._rows())
                    return observation

                self.provider.recover_embedding = recover
                self._error('EMBEDDING_LEASE_LOST', self.jobs.reconcile, *self.scope, self.provider, lambda: True)
                current = self.jobs.status(*self.scope)[0]
                self.assertEqual((current['status'], current['request_id'], current['attempt']),
                    ('UNKNOWN', successor[0]['request_id'], 1))
                self.assertEqual(self._rows(), persisted[0])
                self.assertEqual(self.provider.calls, [])

    def test_retry_and_circuit_equality_admit_but_before_deadline_blocks(self):
        self.jobs = EmbeddingJobs(self.repository, circuit_threshold=1)
        row, dependency = self._claim()
        error = RuntimeError('synthetic 429')
        error.status, error.retry_after_seconds = 429, self.jobs.circuit_seconds
        rejected = self._error('EMBEDDING_RETRY', self.jobs._finish, row, error=error)
        self.clock = rejected.retry_at - timedelta(milliseconds=1)
        self._error('EMBEDDING_RETRY', self.jobs._claim, *self.scope, 0, dependency)
        self.clock = rejected.retry_at
        second = self.jobs._claim(*self.scope, 0, dependency)
        self.assertEqual(second['attempt'], 2)
        self.jobs._finish(second, vectors=[[1., 0.]])
        circuit = self._rows()[2][0]
        self.assertEqual((circuit['failures'], circuit['open_until']), (0, None))

    def test_open_circuit_blocks_other_batch_without_allocating_request(self):
        self.jobs = EmbeddingJobs(self.repository, circuit_threshold=1)
        row, _ = self._claim()
        error = RuntimeError('synthetic 500')
        error.status = 500
        self._error('EMBEDDING_RETRY', self.jobs._finish, row, error=error)
        self.scope = ('tenant', 'kb', 'other')
        _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
        before = self._rows()
        self._error('EMBEDDING_CIRCUIT_OPEN', self.jobs._claim, *self.scope, 0, dependency)
        self.assertEqual(self._rows(), before)
        self.clock = before[2][0]['open_until']
        self.assertEqual(self.jobs._claim(*self.scope, 0, dependency)['attempt'], 1)

    def test_retry_cap_and_preflight_classification_preserve_circuit_accounting(self):
        self.jobs = EmbeddingJobs(self.repository, max_attempts=1, circuit_threshold=1)
        row, dependency = self._claim()
        error = RuntimeError('synthetic HTTP')
        error.status = 503
        self._error('EMBEDDING_ERROR', self.jobs._finish, row, error=error)
        self.clock += timedelta(seconds=self.jobs.circuit_seconds)
        self._error('EMBEDDING_ERROR', self.jobs._claim, *self.scope, 0, dependency)
        for code, status, attempt in (('EMBEDDING_INPUT_TOO_LARGE', 'ERROR', 1),
                                      ('EMBEDDING_BUDGET_INSUFFICIENT', 'RETRY', 0),
                                      ('EMBEDDING_LEDGER_BLOCKED', 'RETRY', 0)):
            with self.subTest(code=code):
                self.scope = ('tenant', 'kb', code)
                row, _ = self._claim()
                before = self._rows()[2]
                self._error(code, self.jobs._finish, row, error=EmbeddingPreflightError(code))
                current = self.jobs.status(*self.scope)[0]
                self.assertEqual((current['status'], current['attempt']), (status, attempt))
                self.assertEqual(self._rows()[2], before)

    def test_recoverable_persisted_error_claim_refunds_only_prior_preflight_attempt(self):
        row, dependency = self._claim()
        with self.jobs._tx(*self.scope[:2]) as transaction:
            transaction._transaction.execute(update(EMBEDDING_JOBS).values(
                status='ERROR', error='EMBEDDING_BUDGET_INSUFFICIENT'))
            transaction._transaction.execute(update(EMBEDDING_ATTEMPTS).values(status='ERROR'))
        successor = self.jobs._claim(*self.scope, 0, dependency)
        self.assertEqual(successor['attempt'], 1)
        self.assertNotEqual(successor['request_id'], row['request_id'])
        self.assertEqual({item['request_id']: item['status'] for item in self._rows()[1]},
                         {row['request_id']: 'ERROR', successor['request_id']: 'RUNNING'})

    def test_recovery_rejects_mismatched_or_nonzero_unsent_observations(self):
        row, dependency = self._claim()
        self.clock = row['lease_until']
        self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
        original = self._observation(row, unsent=True)
        mutants = [('physical_request_id', 'other'), ('texts', ['other']), ('audit_context', {}),
                   ('provider_manifest', {}), ('physical_embedding_attempts', True),
                   ('physical_embedding_attempts', 1), ('usage', {'calls': 1}), ('reserved', {})]
        for key, value in mutants:
            with self.subTest(key=key, value=value):
                observation = deepcopy(original)
                observation['record'][key] = value
                self.provider.recover_embedding = lambda request: observation
                before = self._rows()
                self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
                self.assertEqual(self._rows(), before)
        self.provider.recover_embedding = lambda request: original
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['attempt'], 0)
        self.assertEqual(self.provider.calls, [])

    def test_lost_claim_acknowledgment_does_not_make_request_replayable(self):
        _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
        original = self.jobs._tx

        @contextmanager
        def lost_ack(*args):
            with original(*args) as transaction:
                yield transaction
            raise OSError('synthetic lost commit acknowledgment')

        with patch.object(self.jobs, '_tx', lost_ack):
            with self.assertRaisesRegex(OSError, 'lost commit acknowledgment'):
                self.jobs._claim(*self.scope, 0, dependency)
        restarted = EmbeddingJobs(self.repository)
        row = restarted.status(*self.scope)[0]
        self.assertEqual((row['status'], row['attempt']), ('RUNNING', 1))
        self._error('EMBEDDING_RUNNING', restarted.run, *self.scope, self.provider, self.units, lambda: True, self.audit)
        self.clock = row['lease_until']
        self._error('EMBEDDING_UNKNOWN', restarted.run, *self.scope, self.provider, self.units, lambda: True, self.audit)
        self.assertEqual(self.provider.calls, [])

    def test_lost_finish_acknowledgment_reuses_durable_result_without_send(self):
        row, _ = self._claim()
        original = self.jobs._tx

        @contextmanager
        def lost_ack(*args):
            with original(*args) as transaction:
                yield transaction
            raise OSError('synthetic lost commit acknowledgment')

        with patch.object(self.jobs, '_tx', lost_ack):
            with self.assertRaisesRegex(OSError, 'lost commit acknowledgment'):
                self.jobs._finish(row, vectors=[[1., 0.]])
        restarted = EmbeddingJobs(self.repository)
        self.assertEqual(restarted.run(*self.scope, self.provider, self.units, lambda: True, self.audit),
            [{'text': 'synthetic', 'vector': [1., 0.]}])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self._rows()[1][0]['status'], 'COMPLETE')

    def test_circuit_statement_failure_rolls_back_job_and_attempt(self):
        row, _ = self._claim()
        original = KnowledgeTransaction.execute
        before = self._rows()

        def fail(transaction, query, params=None):
            if query == K.CIRCUIT_FAILURE:
                raise OSError('synthetic circuit write failure')
            return original(transaction, query, params)

        with patch.object(KnowledgeTransaction, 'execute', fail):
            with self.assertRaisesRegex(OSError, 'circuit write failure'):
                self.jobs._finish(row, error=TimeoutError())
        self.assertEqual(self._rows(), before)

    def test_recovery_snapshot_cannot_rewrite_same_request_terminal_state(self):
        row, dependency = self._claim()
        self.clock = row['lease_until']
        self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
        observation = self._observation(row)

        def recover(request):
            self.jobs._finish(row, vectors=[[1., 0.]], recovered=True)
            return observation

        self.provider.recover_embedding = recover
        self._error('EMBEDDING_LEASE_LOST', self.jobs.reconcile, *self.scope, self.provider, lambda: True)
        self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'COMPLETE')
        self.assertEqual(self._rows()[1][0]['status'], 'COMPLETE')
        self.assertEqual(self.provider.calls, [])

    def test_complete_recovery_rejects_mismatched_ledger_and_corrupt_vectors(self):
        row, dependency = self._claim()
        self.clock = row['lease_until']
        self._error('EMBEDDING_UNKNOWN', self.jobs._claim, *self.scope, 0, dependency)
        original = self._observation(row)
        for key, value in (('physical_status', 'ERROR'), ('physical_usage', {'calls': 0}),
                           ('vectors', [[0., 0.]]), ('vectors_sha256', 'unrelated')):
            with self.subTest(key=key):
                observation = deepcopy(original)
                target = observation if key.startswith('physical_') else observation['record']
                target[key] = value
                self.provider.recover_embedding = lambda request: observation
                before = self._rows()
                self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
                self.assertEqual(self._rows(), before)
        self.provider.recover_embedding = lambda request: original
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'COMPLETE')
        self.assertEqual(self.provider.calls, [])

    def test_known_unsent_http_backoff_refunds_without_circuit_failure(self):
        row, dependency = self._claim()
        error = RuntimeError('synthetic known-unsent HTTP')
        error.model_sent, error.status, error.retry_after_seconds = False, 429, 10
        outcome = self._error('EMBEDDING_RETRY', self.jobs._finish, row, error=error)
        self.assertEqual(outcome.retry_at, self.clock + timedelta(seconds=10))
        current = self.jobs.status(*self.scope)[0]
        self.assertEqual(current['attempt'], 0)
        self.assertEqual(self._rows()[2][0]['failures'], 0)
        self._error('EMBEDDING_RETRY', self.jobs._claim, *self.scope, 0, dependency)
        self.clock = outcome.retry_at
        self.assertEqual(self.jobs._claim(*self.scope, 0, dependency)['attempt'], 1)

    def test_default_retry_budget_and_circuit_threshold_are_not_weakened(self):
        row, dependency = self._claim()
        for attempt in range(1, 4):
            self.assertEqual(row['attempt'], attempt)
            error = RuntimeError('synthetic retryable HTTP')
            error.status = 500
            outcome = self._error('EMBEDDING_RETRY' if attempt < 3 else 'EMBEDDING_ERROR',
                                  self.jobs._finish, row, error=error)
            circuit = self._rows()[2][0]
            self.assertEqual(circuit['failures'], attempt)
            self.assertEqual(circuit['open_until'], None if attempt < 3 else
                             self.clock + timedelta(seconds=self.jobs.circuit_seconds))
            self.assertEqual(outcome.retry_at, self.clock + timedelta(seconds=2 ** attempt))
            self.clock = outcome.retry_at
            if attempt < 3:
                row = self.jobs._claim(*self.scope, 0, dependency)
            else:
                self._error('EMBEDDING_ERROR', self.jobs._claim, *self.scope, 0, dependency)
        self.assertEqual(len(self._rows()[1]), 3)

    def test_ambiguous_failure_can_recover_later_evidence_and_reset_circuit(self):
        row, _ = self._claim()
        self._error('EMBEDDING_UNKNOWN', self.jobs._finish, row, error=TimeoutError())
        self.assertEqual(self._rows()[2][0]['failures'], 1)
        self.provider.recover_embedding = lambda request: self._observation(row)
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'COMPLETE')
        circuit = self._rows()[2][0]
        self.assertEqual((circuit['failures'], circuit['open_until']), (0, None))
        self.assertEqual(self.provider.calls, [])

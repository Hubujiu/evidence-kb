from contextlib import closing, contextmanager, ExitStack
import json
import sqlite3
from os import getpid
from pathlib import Path
from threading import local

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..database_contracts import DatabaseError
from ..persistence_local import local_database, read_frozen_ledger, sealed_local_ledger
from ..persistence_schema import budget_calls, budget_config, budget_guard, budget_schema


BUDGET_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SQLAlchemyError, DatabaseError)

@contextmanager
def producer_guard(path, *, allow_sealed=False):
    target = Path(str(Path(path).resolve()) + '.producer.sqlite3')
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target, timeout=0)) as db:
        try:
            db.execute('CREATE TABLE IF NOT EXISTS producer (id INTEGER PRIMARY KEY, successor TEXT)')
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            raise ValueError('BUDGET_PRODUCER_BUSY') from exc
        try:
            row = db.execute('SELECT successor FROM producer WHERE id=1').fetchone()
            if row and not allow_sealed:
                raise ValueError('BUDGET_PRODUCER_SEALED')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def validate_caps(caps):
    if not isinstance(caps, dict) or not caps or any(type(value) is not int or value < 0 for value in caps.values()):
        raise ValueError('caps must be nonnegative integer resources')


def validate_resources(caps, resources, *, complete):
    if not isinstance(resources, dict) or not set(resources) <= caps.keys() or (complete and set(resources) != caps.keys()):
        raise ValueError('resource dimensions do not match frozen budget')
    if any(type(value) is not int or value < 0 for value in resources.values()):
        raise ValueError('resource values must be nonnegative integers')
def validate_ledger_configuration(path, caps):
    validate_caps(caps)
    path = Path(path)
    if not path.exists():
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as database:
        row = database.execute('SELECT caps FROM config WHERE id=1').fetchone()
        if row and json.loads(row[0]) != caps:
            raise ValueError('cannot silently change frozen budget caps')




class BudgetLedger:
    def __init__(self, path: Path | None = None, caps: dict[str, int] | None = None, *, database=None):
        validate_caps(caps)
        caps = dict(caps)
        self.path = None if path is None else Path(path)
        self._owned, self.database = None, database
        self._producer = local()
        self._readonly = database is None and self.path is not None and sealed_local_ledger(self.path)
        if self._readonly:
            validate_ledger_configuration(self.path, caps)
            self.caps = caps
            return
        if self.database is None:
            if self.path is None:
                raise ValueError('BUDGET_STORAGE_REQUIRED')
            self.database, self._owned = local_database(self.path, owner='physical-budget')
        elif self.path is not None:
            raise ValueError('BUDGET_PATH_AND_DATABASE_ARE_EXCLUSIVE')
        try:
            self.database.prepare(budget_schema(self.database.identity.space.owner_id))
            self.database.seal()
            with self.database.transaction(write=True) as db:
                db.insert(budget_config, {'id': 1, 'caps': json.dumps(caps)}, conflict_keys=('id',))
                db.insert(budget_guard, {'id': 1, 'successor': None}, conflict_keys=('id',))
                current = db.execute(select(budget_config.c.caps).where(budget_config.c.id == 1)
                                     .with_for_update()).fetchone()
                if json.loads(current['caps']) != caps:
                    raise ValueError('cannot silently change frozen budget caps')
                self.caps = json.loads(current['caps'])
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._owned is not None:
            self._owned.close()
            self._owned = None

    @contextmanager
    def _db(self, *, write=True):
        if self._readonly:
            raise ValueError('BUDGET_PRODUCER_SEALED')
        with self.database.transaction(write=write) as db:
            if write:
                db.execute(select(budget_config.c.caps).where(budget_config.c.id == 1).with_for_update())
            yield db

    @contextmanager
    def admission(self):
        if getattr(self._producer, 'owner', None) == getpid():
            yield
            return
        guard = producer_guard(self.path) if self.path is not None else self._remote_guard()
        with guard:
            self._producer.owner = getpid()
            try:
                yield
            finally:
                self._producer.owner = None

    @contextmanager
    def _remote_guard(self):
        with ExitStack() as stack:
            try:
                stack.enter_context(self.database.guard('budget-producer', nowait=True))
            except DatabaseError as error:
                if error.code == 'DATABASE_GUARD_BUSY':
                    raise ValueError('BUDGET_PRODUCER_BUSY') from error
                raise
            with self.database.transaction(write=False) as db:
                row = db.execute(select(budget_guard.c.successor).where(budget_guard.c.id == 1)).fetchone()
            if row is None or row['successor'] is not None:
                raise ValueError('BUDGET_PRODUCER_SEALED')
            yield

    def _valid(self, resources, *, complete):
        validate_resources(self.caps, resources, complete=complete)

    @staticmethod
    def _totals(db, caps):
        result = db.execute(select(budget_calls.c.reserved, budget_calls.c.usage))
        return BudgetLedger._sum_rows(result.fetchall(), caps)

    @staticmethod
    def _sum_rows(rows, caps):
        total = dict.fromkeys(caps, 0)
        for row in rows:
            reserved, usage = ((row['reserved'], row['usage']) if hasattr(row, 'keys') else row)
            reserve = json.loads(reserved)
            actual = json.loads(usage) if usage else {}
            for resource in total:
                total[resource] += actual.get(resource, reserve[resource])
        return total

    def reserve(self, request_id, resources, category="runtime", *, require_settled=False):
        with self.admission():
            return self._reserve(request_id, resources, category, require_settled=require_settled)

    def _reserve(self, request_id, resources, category, *, require_settled):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError('INVALID_PHYSICAL_REQUEST_ID')
        self._valid(resources, complete=True)
        with self._db() as db:
            if require_settled and db.execute(
                    select(budget_calls.c.id).where(budget_calls.c.status.in_(('STARTED', 'UNKNOWN')))
                    .limit(1)).fetchone():
                raise ValueError("UNRESOLVED_PHYSICAL_REQUEST")
            if db.execute(select(budget_calls.c.id).where(budget_calls.c.id == request_id)).fetchone():
                raise ValueError("physical request already registered; inspect its result, never resend")
            used = self._totals(db, self.caps)
            if any(used[k] + resources[k] > cap for k, cap in self.caps.items()):
                raise ValueError("BUDGET_INSUFFICIENT")
            db.insert(budget_calls, {'id': request_id, 'status': 'STARTED', 'reserved': json.dumps(resources),
                'usage': None, 'category': category, 'diagnostic': None})
        return request_id

    def finish(self, request_id, *, status, usage=None, diagnostic=None):
        if status not in ("COMPLETE", "ERROR", "UNKNOWN"):
            raise ValueError("invalid physical request state")
        if usage is not None:
            self._valid(usage, complete=False)
        with self._db() as db:
            cursor = db.execute(budget_calls.update().where(budget_calls.c.id == request_id,
                budget_calls.c.status == 'STARTED').values(status=status,
                usage=json.dumps(usage) if usage is not None else None, diagnostic=diagnostic))
            if cursor.rowcount != 1:
                raise ValueError("missing or terminal physical request")

    def _records(self, identities=None):
        try:
            if self._readonly:
                return read_frozen_ledger(self.path, identities)
            with self._db(write=False) as db:
                statement = select(budget_calls)
                if identities is not None:
                    statement = statement.where(budget_calls.c.id.in_(identities))
                return [dict(row) for row in db.execute(statement).fetchall()]
        except (sqlite3.Error, SQLAlchemyError, DatabaseError) as error:
            raise ValueError('BUDGET_READ_UNAVAILABLE') from error

    def entry(self, request_id):
        rows = self._records((request_id,))
        if not rows:
            return None
        result = rows[0]
        for column in ('reserved', 'usage'):
            result[column] = None if result[column] is None else json.loads(result[column])
        return result

    def registered(self, attempts):
        attempts = tuple(attempts)
        if not attempts:
            return []
        found = set()
        for start in range(0, len(attempts), 500):
            found.update(row['id'] for row in self._records(attempts[start:start + 500]))
        return [identity for identity in attempts if identity in found]

    def summary(self):
        rows = self._records()
        normalized = [(row['status'], row['reserved'], row['usage']) for row in rows]
        totals = self._sum_rows([row[1:] for row in normalized], self.caps)
        return {"physical_requests": len(rows), "effective_usage_or_reserved": totals,
                "unknown_usage_requests": sum(u is None or set(json.loads(u)) != set(self.caps) for _, _, u in normalized),
                "pending_or_unknown": sum(s in ("STARTED", "UNKNOWN") for s, _, _ in normalized),
                "over_budget": any(totals[k] > cap for k, cap in self.caps.items())}

    def status_snapshot(self):
        try:
            current = self.summary()
        except BUDGET_READ_ERRORS:
            return {'current': None, 'cumulative': None, 'admission_pending_or_unknown': None,
                    'reviewed_unknown_requests': 0, 'blocked': True,
                    'blocked_reason': 'BUDGET_READ_UNAVAILABLE'}
        return {'current': current, 'cumulative': current,
                'admission_pending_or_unknown': current['pending_or_unknown'],
                'reviewed_unknown_requests': 0,
                'blocked': bool(current['pending_or_unknown'] or current['over_budget']),
                'blocked_reason': None}

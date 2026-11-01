from contextlib import closing, contextmanager
import json
import sqlite3
from os import getpid
from pathlib import Path
from threading import local

from sqlalchemy.exc import SQLAlchemyError

from ..persistence_local import local_database, sealed_local_ledger
from ..persistence_schema import budget_schema

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
        self._usage_column = 'usage'
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
        if self.database.info.engine == 'sqlite':
            self.path = Path(self.database.physical_name)
        if self.database.info.engine == 'mysql':
            self._usage_column = '`usage`'
        try:
            self.database.prepare(budget_schema(self.database.identity.space.owner_id, self.database.info.engine))
            self.database.seal()
            insert = ('INSERT IGNORE INTO config(id,caps) VALUES(1,:caps)' if self.database.info.engine == 'mysql'
                      else 'INSERT INTO config(id,caps) VALUES(1,:caps) ON CONFLICT(id) DO NOTHING')
            with self.database.transaction(write=True) as db:
                db.execute(insert, {'caps': json.dumps(caps)})
                current = db.execute('SELECT caps FROM config WHERE id=1' + self._lock_suffix()).fetchone()
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

    def _lock_suffix(self):
        return '' if self.database.info.engine == 'sqlite' else ' FOR UPDATE'

    @contextmanager
    def _db(self, *, write=True):
        if self._readonly:
            if write:
                raise ValueError('BUDGET_PRODUCER_SEALED')
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                yield db
            return
        with self.database.transaction(write=write) as db:
            if write:
                db.execute('SELECT caps FROM config WHERE id=1' + self._lock_suffix())
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
        with self.database.transaction(write=True) as db:
            try:
                row = db.execute('SELECT successor FROM producer_guard WHERE id=1 FOR UPDATE NOWAIT').fetchone()
            except Exception as error:
                raise ValueError('BUDGET_PRODUCER_BUSY') from error
            if row is None or row['successor'] is not None:
                raise ValueError('BUDGET_PRODUCER_SEALED')
            yield

    def _valid(self, resources, *, complete):
        validate_resources(self.caps, resources, complete=complete)

    @staticmethod
    def _totals(db, caps):
        result = db.execute('SELECT reserved, calls.usage FROM calls')
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
                    "SELECT 1 FROM calls WHERE status IN ('STARTED','UNKNOWN') LIMIT 1").fetchone():
                raise ValueError("UNRESOLVED_PHYSICAL_REQUEST")
            if db.execute('SELECT 1 FROM calls WHERE id=:id', {'id': request_id}).fetchone():
                raise ValueError("physical request already registered; inspect its result, never resend")
            used = self._totals(db, self.caps)
            if any(used[k] + resources[k] > cap for k, cap in self.caps.items()):
                raise ValueError("BUDGET_INSUFFICIENT")
            db.execute('INSERT INTO calls(id,status,reserved,' + self._usage_column + ',category,diagnostic) '
                       "VALUES(:id,'STARTED',:reserved,NULL,:category,NULL)",
                       {'id': request_id, 'reserved': json.dumps(resources), 'category': category})
        return request_id

    def finish(self, request_id, *, status, usage=None, diagnostic=None):
        if status not in ("COMPLETE", "ERROR", "UNKNOWN"):
            raise ValueError("invalid physical request state")
        if usage is not None:
            self._valid(usage, complete=False)
        with self._db() as db:
            cursor = db.execute('UPDATE calls SET status=:status,' + self._usage_column + '=:usage,diagnostic=:diagnostic '
                                "WHERE id=:id AND status='STARTED'", {'status': status,
                                'usage': json.dumps(usage) if usage is not None else None,
                                'diagnostic': diagnostic, 'id': request_id})
            if cursor.rowcount != 1:
                raise ValueError("missing or terminal physical request")

    @contextmanager
    def _reading(self):
        try:
            with self._db(write=False) as db:
                yield db
        except (sqlite3.Error, SQLAlchemyError) as error:
            raise ValueError('BUDGET_READ_UNAVAILABLE') from error

    def entry(self, request_id):
        columns = ('id', 'status', 'reserved', 'usage', 'category', 'diagnostic')
        with self._reading() as db:
            row = db.execute('SELECT id,status,reserved,calls.usage,category,diagnostic FROM calls WHERE id=:id',
                             {'id': request_id}).fetchone()
        if row is None:
            return None
        result = dict(row) if hasattr(row, 'keys') else dict(zip(columns, row, strict=True))
        if isinstance(result['id'], bytes):
            result['id'] = result['id'].decode('utf-8')
        for column in ('reserved', 'usage'):
            result[column] = None if result[column] is None else json.loads(result[column])
        return result

    def registered(self, attempts):
        attempts = tuple(attempts)
        if not attempts:
            return []
        found = set()
        with self._reading() as db:
            for start in range(0, len(attempts), 500):
                values = {f'id_{offset}': value for offset, value in enumerate(attempts[start:start + 500])}
                rows = db.execute('SELECT id FROM calls WHERE id IN (' +
                                  ','.join(':' + key for key in values) + ')', values).fetchall()
                for row in rows:
                    identity = row['id'] if hasattr(row, 'keys') else row[0]
                    found.add(identity.decode('utf-8') if isinstance(identity, bytes) else identity)
        return [identity for identity in attempts if identity in found]

    def summary(self):
        with self._db(write=False) as db:
            rows = db.execute('SELECT status, reserved, calls.usage FROM calls').fetchall()
            normalized = [(row['status'], row['reserved'], row['usage']) if hasattr(row, 'keys') else row
                          for row in rows]
            totals = self._sum_rows([row[1:] for row in normalized], self.caps)
        return {"physical_requests": len(rows), "effective_usage_or_reserved": totals,
                "unknown_usage_requests": sum(u is None or set(json.loads(u)) != set(self.caps) for _, _, u in normalized),
                "pending_or_unknown": sum(s in ("STARTED", "UNKNOWN") for s, _, _ in normalized),
                "over_budget": any(totals[k] > cap for k, cap in self.caps.items())}

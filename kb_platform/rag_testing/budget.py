from contextlib import closing
import json
import sqlite3
from pathlib import Path


class BudgetLedger:
    def __init__(self, path: Path, caps: dict[str, int]):
        if not caps or any(type(v) is not int or v < 0 for v in caps.values()):
            raise ValueError("caps must be nonnegative integer resources")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.caps = path, caps
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, caps TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, status TEXT, reserved TEXT, "
                       "usage TEXT, category TEXT, diagnostic TEXT)")
            current = db.execute("SELECT caps FROM config WHERE id=1").fetchone()
            if current and json.loads(current[0]) != caps:
                raise ValueError("cannot silently change frozen budget caps")
            db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (json.dumps(caps),))

    def _valid(self, resources, *, complete):
        if not set(resources) <= self.caps.keys() or (complete and set(resources) != self.caps.keys()):
            raise ValueError("resource dimensions do not match frozen budget")
        if any(type(v) is not int or v < 0 for v in resources.values()):
            raise ValueError("resource values must be nonnegative integers")

    @staticmethod
    def _totals(db, caps):
        total = dict.fromkeys(caps, 0)
        for reserved, usage in db.execute("SELECT reserved, usage FROM calls"):
            reserve = json.loads(reserved)
            actual = json.loads(usage) if usage else {}
            for resource in total:
                total[resource] += actual.get(resource, reserve[resource])
        return total

    def reserve(self, request_id, resources, category="runtime", *, require_settled=False):
        self._valid(resources, complete=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if require_settled and db.execute(
                    "SELECT 1 FROM calls WHERE status IN ('STARTED','UNKNOWN') LIMIT 1").fetchone():
                raise ValueError("UNRESOLVED_PHYSICAL_REQUEST")
            if db.execute("SELECT 1 FROM calls WHERE id=?", (request_id,)).fetchone():
                raise ValueError("physical request already registered; inspect its result, never resend")
            used = self._totals(db, self.caps)
            if any(used[k] + resources[k] > cap for k, cap in self.caps.items()):
                raise ValueError("BUDGET_INSUFFICIENT")
            db.execute("INSERT INTO calls VALUES (?, 'STARTED', ?, NULL, ?, NULL)",
                       (request_id, json.dumps(resources), category))
        return request_id

    def finish(self, request_id, *, status, usage=None, diagnostic=None):
        if status not in ("COMPLETE", "ERROR", "UNKNOWN"):
            raise ValueError("invalid physical request state")
        if usage is not None:
            self._valid(usage, complete=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute("UPDATE calls SET status=?,usage=?,diagnostic=? WHERE id=? AND status='STARTED'",
                                (status, json.dumps(usage) if usage is not None else None, diagnostic, request_id))
            if cursor.rowcount != 1:
                raise ValueError("missing or terminal physical request")

    def summary(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = db.execute("SELECT status, reserved, usage FROM calls").fetchall()
            totals = self._totals(db, self.caps)
        return {"physical_requests": len(rows), "effective_usage_or_reserved": totals,
                "unknown_usage_requests": sum(u is None or set(json.loads(u)) != set(self.caps) for _, _, u in rows),
                "pending_or_unknown": sum(s in ("STARTED", "UNKNOWN") for s, _, _ in rows),
                "over_budget": any(totals[k] > cap for k, cap in self.caps.items())}

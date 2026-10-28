"""Exact immutable stage cache. A cache hit is never an execution measurement."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from pathlib import Path
from typing import Callable

from .contracts import Contract, digest


class StageKey(Contract):
    stage: str
    implementation: str
    upstream: tuple[str, ...]
    config_hash: str
    corpus_hash: str
    index_snapshot: str
    principal: str
    acl_revision: str
    model_revision: str
    sample_id: str


class StageCache:
    def __init__(self, root: Path):
        self.root = root
        (root / "objects").mkdir(parents=True, exist_ok=True)
        self.db = root / "cache.sqlite3"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, hash TEXT NOT NULL)")

    def put(self, key: StageKey, payload):
        content_hash = digest(payload)
        target = self.root / "objects" / (content_hash + ".json")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        # Exclusive creation plus full validation on reads; interrupted writes fail closed.
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(raw)
        except FileExistsError:
            if digest(json.loads(target.read_text("utf-8"))) != content_hash:
                raise ValueError("cache object integrity failure")
        with closing(sqlite3.connect(self.db)) as db, db:
            old = db.execute("SELECT hash FROM entries WHERE key=?", (digest(key),)).fetchone()
            if old and old[0] != content_hash:
                raise ValueError("stage output changed; use a new implementation/sample identity")
            db.execute("INSERT OR IGNORE INTO entries VALUES (?,?)", (digest(key), content_hash))

    def get(self, key: StageKey, *, authorize: Callable[[], bool], mode="replay", under_test=False):
        # Reauthorize even when the old ACL key still happens to be known.
        if not authorize():
            raise PermissionError("current authorization rejected cached artifact")
        if mode not in ("replay", "live", "cold"):
            raise ValueError("unknown cache mode")
        if mode == "cold" or (mode == "live" and under_test):
            return None
        with closing(sqlite3.connect(self.db)) as db, db:
            row = db.execute("SELECT hash FROM entries WHERE key=?", (digest(key),)).fetchone()
        if row is None:
            return None
        payload = json.loads((self.root / "objects" / (row[0] + ".json")).read_text("utf-8"))
        if digest(payload) != row[0]:
            raise ValueError("cache object integrity failure")
        return payload

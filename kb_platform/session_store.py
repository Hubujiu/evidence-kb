from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .session_identity import PlatformError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class SessionStore:


    def __init__(self, root='.local/agent-sessions'):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'sessions.sqlite3'
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, principal TEXT NOT NULL,
                    scope TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    session TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, group_id TEXT NOT NULL, agent TEXT NOT NULL,
                    created TEXT NOT NULL, PRIMARY KEY(session,seq));
                CREATE TABLE IF NOT EXISTS requests (
                    session TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    token TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
                    response TEXT, PRIMARY KEY(session,request_id));
                CREATE TABLE IF NOT EXISTS artifacts (
                    session TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    created TEXT NOT NULL, PRIMARY KEY(session,id));
                CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_artifacts_update BEFORE UPDATE ON artifacts
                    BEGIN SELECT RAISE(ABORT,'immutable artifact'); END;
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _owner(db, actor, session_id):
        row = db.execute('SELECT * FROM sessions WHERE id=? AND tenant=? AND principal=?',
                         (session_id, actor.tenant_id, actor.principal_id)).fetchone()
        if row is None:
            raise PlatformError('SESSION_NOT_FOUND', 'Conversation not available', 404)
        return row

    @staticmethod
    def _decode(row):
        return {'session_id': row['id'], 'scope_id': row['scope'], 'revision': row['revision'],
                'state': json.loads(row['state']), 'created_at': row['created'], 'updated_at': row['updated']}

    def create(self, actor, scope_id, initial_state):

        if not isinstance(initial_state, dict):
            raise ValueError('INVALID_SESSION_STATE')
        session_id = uuid4().hex
        state = initial_state
        timestamp = now()
        with self._db() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',
                       (session_id, actor.tenant_id, actor.principal_id, scope_id, 0,
                        canonical(state), timestamp, timestamp))
        return self.load(actor, session_id)

    def load(self, actor, session_id):
        with self._db() as db:
            return self._decode(self._owner(db, actor, session_id))

    def list_sessions(self, actor, *, limit=50, offset=0):

        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise PlatformError('INVALID_SESSION_PAGE', 'Expected limit1..100 and nonnegative offset', 422)
        with self._db() as db:
            return [{'session_id': row['id'], 'scope_id': row['scope'], 'revision': row['revision'],
                     'created_at': row['created'], 'updated_at': row['updated']}
                    for row in db.execute('SELECT id,scope,revision,created,updated FROM sessions '
                        'WHERE tenant=? AND principal=? ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?',
                        (actor.tenant_id, actor.principal_id, limit, offset))]

    def events(self, actor, session_id):
        with self._db() as db:
            self._owner(db, actor, session_id)
            return [{**dict(row), 'payload': json.loads(row['payload'])} for row in
                    db.execute('SELECT * FROM events WHERE session=? ORDER BY seq', (session_id,))]

    @staticmethod
    def _event(db, session_id, kind, payload, group_id, agent):
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session=?',
                         (session_id,)).fetchone()[0]
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                   (session_id, seq, kind, canonical(payload), group_id, agent, now()))
        return seq

    def begin(self, actor, session_id, request_id, expected_revision, fingerprint, lease_seconds=900):
        with self._db() as db:
            session = self._owner(db, actor, session_id)
            previous = db.execute('SELECT * FROM requests WHERE session=? AND request_id=?',
                                  (session_id, request_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise PlatformError('REQUEST_ID_REUSED', 'Request id has different content', 409)
                if previous['status'] == 'complete':
                    return {'cached': json.loads(previous['response'])}
                raise PlatformError('TURN_IN_PROGRESS' if previous['expires'] > time.time() else 'TURN_INTERRUPTED',
                                    'Request is running or interrupted; inspect its durable events', 409)
            if session['revision'] != expected_revision:
                raise PlatformError('SESSION_REVISION_CONFLICT', 'Reload conversation before submitting', 409)
            pending = db.execute("SELECT * FROM requests WHERE session=? AND status='running'", (session_id,)).fetchall()
            if any(row['expires'] > time.time() for row in pending):
                raise PlatformError('TURN_IN_PROGRESS', 'A conversation has one active writer', 409)
            for row in pending:
                db.execute("UPDATE requests SET status='interrupted' WHERE session=? AND request_id=?",
                           (session_id, row['request_id']))
                self._event(db, session_id, 'interrupted', {'request_id': row['request_id']}, row['request_id'], 'system')
            token = uuid4().hex
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',
                       (session_id, request_id, fingerprint, token, 'running', time.time() + lease_seconds, None))
            return {'token': token, 'session': self._decode(session)}

    @staticmethod
    def _lease(db, session_id, token):
        row = db.execute("SELECT * FROM requests WHERE session=? AND token=? AND status='running'",
                         (session_id, token)).fetchone()
        if row is None or row['expires'] <= time.time():
            raise PlatformError('TURN_LEASE_EXPIRED', 'Conversation writer lease expired', 409)
        return row

    def append(self, actor, session_id, token, kind, payload, group_id, agent='orchestrator'):
        with self._db() as db:
            self._owner(db, actor, session_id)
            self._lease(db, session_id, token)
            return self._event(db, session_id, kind, payload, group_id, agent)

    def artifact(self, actor, session_id, token, body):
        key = digest(body)
        with self._db() as db:
            self._owner(db, actor, session_id)
            self._lease(db, session_id, token)
            db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)', (session_id, key, canonical(body), now()))
        return {'artifact_id': key, 'sha256': key, 'bytes': len(canonical(body).encode())}

    def read_artifact(self, actor, session_id, artifact_id):
        with self._db() as db:
            self._owner(db, actor, session_id)
            row = db.execute('SELECT body FROM artifacts WHERE session=? AND id=?', (session_id, artifact_id)).fetchone()
            if row is None:
                raise PlatformError('ARTIFACT_NOT_FOUND', 'Artifact not available', 404)
            body = json.loads(row[0])
            if digest(body) != artifact_id:
                raise PlatformError('ARTIFACT_INTEGRITY', 'Artifact integrity check failed', 503)
            return body

    def finish(self, actor, session_id, token, state, response, *, assistant_event=None):
        return self._finish(actor, session_id, token, state, response, assistant_event=assistant_event)

    def fail(self, actor, session_id, token, state, response):

        if not isinstance(response, dict) or response.get('status') != 'error':
            raise ValueError('INVALID_FAILURE_RESPONSE')
        return self._finish(actor, session_id, token, state, response, failure=True)

    def _finish(self, actor, session_id, token, state, response, *, failure=False, assistant_event=None):
        with self._db() as db:
            session = self._owner(db, actor, session_id)
            if failure:
                request = db.execute("SELECT * FROM requests WHERE session=? AND token=? AND status='running'",
                                     (session_id, token)).fetchone()
                if request is None:
                    raise PlatformError('TURN_LEASE_EXPIRED', 'Conversation writer was superseded', 409)
                self._event(db, session_id, 'turn_failed', {'code': response.get('stop_reason', 'SESSION_FAILED')},
                            request['request_id'], 'orchestrator')
            else:
                request = self._lease(db, session_id, token)
                if assistant_event is not None:
                    self._event(db, session_id, 'assistant', assistant_event, request['request_id'], 'orchestrator')
            response = {**response, 'session_id': session_id, 'revision': session['revision'] + 1}
            self._event(db, session_id, 'turn_completed', response, request['request_id'], 'orchestrator')
            db.execute('UPDATE sessions SET revision=?,state=?,updated=? WHERE id=?',
                       (response['revision'], canonical(state), now(), session_id))
            db.execute("UPDATE requests SET status='complete',response=? WHERE session=? AND token=?",
                       (canonical(response), session_id, token))
            return response

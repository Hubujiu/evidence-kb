from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from .session_identity import PlatformError
from .session_storage import session_schema, lock_suffix


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class SessionStore:


    def __init__(self, root='.local/agent-sessions', *, database=None):
        self._owned_service = None
        if database is None:
            from .database_contracts import SpaceKey
            from .database_providers import SQLiteProvider
            from .database_service import DatabaseService

            self.root = Path(root).resolve()
            if (self.root / 'sessions.sqlite3').exists():
                raise ValueError('LEGACY_SESSION_DATABASE_REQUIRES_EXPLICIT_IMPORT')
            self._owned_service = DatabaseService()
            self._owned_service.register('local', SQLiteProvider(self.root), owned=True)
            grant = self._owned_service.grant('local', SpaceKey('local', 'conversations', 'history'),
                                               consumer_identity='session-store')
            database = self._owned_service.bind(grant)
        self.database = database
        try:
            database.prepare(session_schema(database.info.engine, database.identity.space.owner_id))
            database.seal()
        except BaseException:
            self.close()
            raise
        self.identity = database.identity

    def close(self):
        if self._owned_service is not None:
            self._owned_service.close()
            self._owned_service = None

    def _db(self, *, write=True):
        return self.database.transaction(write=write)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        return {key: value.decode('utf-8') if isinstance(value, bytes) else value
                for key, value in row.items()}

    def _owner(self, db, actor, session_id, *, lock=True):
        suffix = lock_suffix(self.database.info.engine) if lock else ''
        row = self._row(db.execute('SELECT * FROM sessions WHERE id=:id AND tenant=:tenant '
                                   'AND principal=:principal' + suffix,
                         {'id': session_id, 'tenant': actor.tenant_id,
                          'principal': actor.principal_id}).fetchone())
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
            db.execute('INSERT INTO sessions VALUES(:id,:tenant,:principal,:scope,:revision,:state,:created,:updated)',
                       {'id': session_id, 'tenant': actor.tenant_id, 'principal': actor.principal_id,
                        'scope': scope_id, 'revision': 0, 'state': canonical(state),
                        'created': timestamp, 'updated': timestamp})
        return self.load(actor, session_id)

    def load(self, actor, session_id):
        with self._db(write=False) as db:
            return self._decode(self._owner(db, actor, session_id, lock=False))

    def list_sessions(self, actor, *, limit=50, offset=0):

        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise PlatformError('INVALID_SESSION_PAGE', 'Expected limit1..100 and nonnegative offset', 422)
        with self._db(write=False) as db:
            return [{'session_id': row['id'], 'scope_id': row['scope'], 'revision': row['revision'],
                     'created_at': row['created'], 'updated_at': row['updated']}
                    for row in map(self._row, db.execute('SELECT id,scope,revision,created,updated FROM sessions '
                        'WHERE tenant=:tenant AND principal=:principal ORDER BY updated DESC,id DESC LIMIT :limit OFFSET :offset',
                        {'tenant': actor.tenant_id, 'principal': actor.principal_id,
                         'limit': limit, 'offset': offset}).fetchall())]

    def events(self, actor, session_id):
        with self._db(write=False) as db:
            self._owner(db, actor, session_id, lock=False)
            return [{**dict(row), 'payload': json.loads(row['payload'])} for row in
                    map(self._row, db.execute('SELECT * FROM events WHERE session=:session ORDER BY seq',
                                             {'session': session_id}).fetchall())]

    @staticmethod
    def _event(db, session_id, kind, payload, group_id, agent):
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM events WHERE session=:session',
                         {'session': session_id}).fetchone()['next_seq']
        db.execute('INSERT INTO events VALUES(:session,:seq,:kind,:payload,:group_id,:agent,:created)',
                   {'session': session_id, 'seq': seq, 'kind': kind, 'payload': canonical(payload),
                    'group_id': group_id, 'agent': agent, 'created': now()})
        return seq

    def begin(self, actor, session_id, request_id, expected_revision, fingerprint, lease_seconds=900):
        with self._db() as db:
            session = self._owner(db, actor, session_id)
            previous = self._row(db.execute('SELECT * FROM requests WHERE session=:session AND request_id=:request',
                                  {'session': session_id, 'request': request_id}).fetchone())
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise PlatformError('REQUEST_ID_REUSED', 'Request id has different content', 409)
                if previous['status'] == 'complete':
                    return {'cached': json.loads(previous['response'])}
                raise PlatformError('TURN_IN_PROGRESS' if previous['expires'] > time.time() else 'TURN_INTERRUPTED',
                                    'Request is running or interrupted; inspect its durable events', 409)
            if session['revision'] != expected_revision:
                raise PlatformError('SESSION_REVISION_CONFLICT', 'Reload conversation before submitting', 409)
            pending = [self._row(row) for row in db.execute(
                "SELECT * FROM requests WHERE session=:session AND status='running'",
                {'session': session_id}).fetchall()]
            if any(row['expires'] > time.time() for row in pending):
                raise PlatformError('TURN_IN_PROGRESS', 'A conversation has one active writer', 409)
            for row in pending:
                db.execute("UPDATE requests SET status='interrupted' WHERE session=:session AND request_id=:request",
                           {'session': session_id, 'request': row['request_id']})
                self._event(db, session_id, 'interrupted', {'request_id': row['request_id']}, row['request_id'], 'system')
            token = uuid4().hex
            db.execute('INSERT INTO requests VALUES(:session,:request,:fingerprint,:token,:status,:expires,:response)',
                       {'session': session_id, 'request': request_id, 'fingerprint': fingerprint,
                        'token': token, 'status': 'running', 'expires': time.time() + lease_seconds, 'response': None})
            return {'token': token, 'session': self._decode(session)}

    def _lease(self, db, session_id, token):
        row = self._row(db.execute("SELECT * FROM requests WHERE session=:session AND token=:token AND status='running'",
                         {'session': session_id, 'token': token}).fetchone())
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
            previous = db.execute('SELECT body FROM artifacts WHERE session=:session AND id=:id',
                                  {'session': session_id, 'id': key}).fetchone()
            if previous is None:
                db.execute('INSERT INTO artifacts VALUES(:session,:id,:body,:created)',
                           {'session': session_id, 'id': key, 'body': canonical(body), 'created': now()})
            elif digest(json.loads(previous['body'])) != key:
                raise PlatformError('ARTIFACT_INTEGRITY', 'Artifact integrity check failed', 503)
        return {'artifact_id': key, 'sha256': key, 'bytes': len(canonical(body).encode())}

    def read_artifact(self, actor, session_id, artifact_id):
        with self._db(write=False) as db:
            self._owner(db, actor, session_id, lock=False)
            row = db.execute('SELECT body FROM artifacts WHERE session=:session AND id=:id',
                             {'session': session_id, 'id': artifact_id}).fetchone()
            if row is None:
                raise PlatformError('ARTIFACT_NOT_FOUND', 'Artifact not available', 404)
            body = json.loads(row['body'])
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
                request = self._row(db.execute("SELECT * FROM requests WHERE session=:session AND token=:token AND status='running'",
                                     {'session': session_id, 'token': token}).fetchone())
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
            db.execute('UPDATE sessions SET revision=:revision,state=:state,updated=:updated WHERE id=:id',
                       {'revision': response['revision'], 'state': canonical(state), 'updated': now(), 'id': session_id})
            db.execute("UPDATE requests SET status='complete',response=:response WHERE session=:session AND token=:token",
                       {'response': canonical(response), 'session': session_id, 'token': token})
            return response

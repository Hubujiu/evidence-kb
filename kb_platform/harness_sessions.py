"""Per-run journal provider; durable application history belongs to its own plugin."""
from copy import deepcopy
from dataclasses import dataclass, field

from .agent_engine import current_execution
from .agent_plugins import ToolContext
from .session_identity import PlatformError, SessionPersistenceError
from .session_store import digest


GENERIC_SESSION = 'agent.generic/1'


def _persist(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except PlatformError:
        raise
    except Exception as error:
        raise SessionPersistenceError('Session persistence failed') from error


@dataclass(frozen=True)
class SessionToolContext(ToolContext):
    expected_revision: int = field(kw_only=True)


def _check_execution():
    execution = current_execution()
    if execution is not None:
        execution.check()


def _pending_tools(events):
    pending = {}
    for event in events:
        if event['kind'] not in {'message', 'assistant'}:
            continue
        record, group = event['payload'], event['group_id']
        if record.get('role') == 'assistant' and 'tool_call' in record:
            if group in pending:
                raise PlatformError('SESSION_HISTORY_INVALID', 'Tool history is inconsistent', 409)
            pending[group] = record['tool_call']['name']
        elif record.get('role') == 'tool':
            if pending.pop(group, None) != record.get('name'):
                raise PlatformError('SESSION_HISTORY_INVALID', 'Tool history is inconsistent', 409)
    return pending


def _unknown_outcome():
    return PlatformError('SESSION_OUTCOME_UNKNOWN',
                         'A recorded tool call has no result; inspect its outcome before continuing', 409)


class DurableSessions:
    """Generic conversation policy over a replaceable transactional store.

    Successful tool receipts survive failed turns. A recorded tool call without
    a receipt blocks new work rather than allowing an ambiguous effect to repeat.
    """

    def __init__(self, store):
        self.store = store

    def create(self, actor, scope_id='default'):
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError('INVALID_AGENT_SCOPE')
        return self.store.create(actor, scope_id,
                                 {'namespace': GENERIC_SESSION, 'turn_number': 0})

    def load(self, actor, session_id):
        session = self.store.load(actor, session_id)
        if session['state'].get('namespace') != GENERIC_SESSION:
            raise PlatformError('SESSION_KIND_MISMATCH', 'Session belongs to another application', 409)
        return session

    def history(self, actor, session_id):
        self.load(actor, session_id)
        return {'session_id': session_id, 'events': self.store.events(actor, session_id)}

    def begin(self, context, message):
        self.load(context.actor, context.session_id)
        _check_execution()
        events = _persist(self.store.events, context.actor, context.session_id)
        settled = {event['group_id'] for event in events if event['kind'] == 'turn_completed'}
        if _pending_tools(events) and context.turn_id not in settled:
            raise _unknown_outcome()
        fingerprint = digest({'namespace': GENERIC_SESSION, 'message': message,
                              'expected_revision': context.expected_revision})
        admission = self.store.begin(context.actor, context.session_id, context.turn_id,
                                     context.expected_revision, fingerprint)
        session = DurableSession(self.store, context, admission)
        if 'cached' in admission:
            return session
        try:
            session.start(message)
        except BaseException as error:
            try:
                session.fail(error)
            except BaseException as cleanup:
                error.add_note(f'Session initialization cleanup: {cleanup}')
            raise
        return session


class DurableSession:
    def __init__(self, store, context, admission):
        self.store, self.context = store, context
        self.start_result = ({'cached': deepcopy(admission['cached'])} if 'cached' in admission else {})
        self._token = admission.get('token')
        self._state = deepcopy(admission.get('session', {}).get('state', {}))
        self._records, self._final = [], None
        self._closed = False

    def start(self, message):
        events = _persist(self.store.events, self.context.actor, self.context.session_id)
        if _pending_tools(events):
            raise _unknown_outcome()
        self._records = []
        for event in events:
            if event['kind'] in {'message', 'assistant'}:
                self._records.append(deepcopy(event['payload']))
            elif event['kind'] == 'turn_completed' and event['payload'].get('status') == 'error':
                self._records.append({'role': 'assistant', 'source': 'runtime', 'content': {
                    'runtime_status': 'failed', 'code': event['payload']['stop_reason']}})
            elif event['kind'] == 'interrupted':
                self._records.append({'role': 'assistant', 'source': 'runtime', 'content': {
                    'runtime_status': 'interrupted'}})
        self.append([{'role': 'user', 'content': deepcopy(message)}])

    def messages(self):
        return deepcopy(self._records)

    def append(self, records):
        if self._closed or self._token is None:
            raise RuntimeError('SESSION_CLOSED')
        for record in records:
            _check_execution()
            copied = deepcopy(record)
            if copied.get('role') == 'assistant' and 'tool_call' not in copied:
                # The terminal assistant message is published atomically with
                # state and the completed response, after native admission.
                self._final = copied
            else:
                _persist(self.store.append, self.context.actor, self.context.session_id, self._token,
                         'message', copied, self.context.turn_id, 'agent')
            self._records.append(copied)

    def finish(self, result):
        if self._closed:
            raise RuntimeError('SESSION_CLOSED')
        if 'cached' in self.start_result:
            self._closed = True
            return deepcopy(self.start_result['cached'])
        _check_execution()
        state = {**self._state, 'turn_number': self._state['turn_number'] + 1}
        response = _persist(self.store.finish, self.context.actor, self.context.session_id, self._token,
                            state, {'status': 'complete', 'result': deepcopy(result)},
                            assistant_event=self._final)
        self._closed = True
        self._records.clear()
        return response

    def fail(self, error):
        if self._closed or self._token is None:
            return
        response = _persist(self.store.fail, self.context.actor, self.context.session_id, self._token,
                            self._state, {'status': 'error',
                                          'stop_reason': getattr(error, 'code', type(error).__name__)})
        self._closed = True
        self._records.clear()
        return response


class MemorySession:
    def __init__(self, message):
        self.start_result = {}
        self._records = [{'role': 'user', 'content': deepcopy(message)}]
        self._closed = False

    def messages(self):
        return deepcopy(self._records)

    def append(self, records):
        if self._closed:
            raise RuntimeError('SESSION_CLOSED')
        self._records.extend(deepcopy(records))

    def finish(self, result):
        self._closed = True
        self._records.clear()

    def fail(self, error):
        self._closed = True
        self._records.clear()


class MemorySessions:
    """A fresh journal per invocation, with no shared evidence or replay cache."""

    def begin(self, context, message):
        return MemorySession(message)

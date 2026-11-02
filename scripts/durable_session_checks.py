import ast
from pathlib import Path


REVIEWED_FUNCTIONS = {'_persist': 'def _persist(operation, *args, **kwargs):\n'
             '    try:\n'
             '        return operation(*args, **kwargs)\n'
             '    except PlatformError:\n'
             '        raise\n'
             '    except Exception as error:\n'
             "        raise SessionPersistenceError('Session persistence failed') from error",
 '_check_execution': 'def _check_execution():\n'
                     '    execution = current_execution()\n'
                     '    if execution is not None:\n'
                     '        execution.check()',
 '_pending_tools': 'def _pending_tools(events):\n'
                   '    pending = {}\n'
                   '    for event in events:\n'
                   "        if event['kind'] not in {'message', 'assistant'}:\n"
                   '            continue\n'
                   "        record, group = (event['payload'], event['group_id'])\n"
                   "        if record.get('role') == 'assistant' and 'tool_call' in record:\n"
                   '            if group in pending:\n'
                   "                raise PlatformError('SESSION_HISTORY_INVALID', 'Tool history is "
                   "inconsistent', 409)\n"
                   "            pending[group] = record['tool_call']['name']\n"
                   "        elif record.get('role') == 'tool':\n"
                   "            if pending.pop(group, None) != record.get('name'):\n"
                   "                raise PlatformError('SESSION_HISTORY_INVALID', 'Tool history is "
                   "inconsistent', 409)\n"
                   '    return pending',
 '_unknown_outcome': 'def _unknown_outcome():\n'
                     "    return PlatformError('SESSION_OUTCOME_UNKNOWN', 'A recorded tool call has no result; "
                     "inspect its outcome before continuing', 409)",
 'DurableSessions.__init__': 'def __init__(self, store):\n    self.store = store',
 'DurableSessions.create': "def create(self, actor, scope_id='default'):\n"
                           '    if not isinstance(scope_id, str) or not scope_id:\n'
                           "        raise ValueError('INVALID_AGENT_SCOPE')\n"
                           "    return self.store.create(actor, scope_id, {'namespace': GENERIC_SESSION, "
                           "'turn_number': 0})",
 'DurableSessions.load': 'def load(self, actor, session_id):\n'
                         '    session = self.store.load(actor, session_id)\n'
                         "    if session['state'].get('namespace') != GENERIC_SESSION:\n"
                         "        raise PlatformError('SESSION_KIND_MISMATCH', 'Session belongs to another "
                         "application', 409)\n"
                         '    return session',
 'DurableSessions.history': 'def history(self, actor, session_id):\n'
                            '    self.load(actor, session_id)\n'
                            "    return {'session_id': session_id, 'events': self.store.events(actor, "
                            'session_id)}',
 'DurableSessions.begin': 'def begin(self, context, message):\n'
                          '    self.load(context.actor, context.session_id)\n'
                          '    _check_execution()\n'
                          '    events = _persist(self.store.events, context.actor, context.session_id)\n'
                          "    settled = {event['group_id'] for event in events if event['kind'] == "
                          "'turn_completed'}\n"
                          '    if _pending_tools(events) and context.turn_id not in settled:\n'
                          '        raise _unknown_outcome()\n'
                          "    fingerprint = digest({'namespace': GENERIC_SESSION, 'message': message, "
                          "'expected_revision': context.expected_revision})\n"
                          '    admission = self.store.begin(context.actor, context.session_id, context.turn_id, '
                          'context.expected_revision, fingerprint)\n'
                          '    session = DurableSession(self.store, context, admission)\n'
                          "    if 'cached' in admission:\n"
                          '        return session\n'
                          '    try:\n'
                          '        session.start(message)\n'
                          '    except BaseException as error:\n'
                          '        try:\n'
                          '            session.fail(error)\n'
                          '        except BaseException as cleanup:\n'
                          "            error.add_note(f'Session initialization cleanup: {cleanup}')\n"
                          '        raise\n'
                          '    return session',
 'DurableSession.__init__': 'def __init__(self, store, context, admission):\n'
                            '    self.store, self.context = (store, context)\n'
                            "    self.start_result = {'cached': deepcopy(admission['cached'])} if 'cached' in "
                            'admission else {}\n'
                            "    self._token = admission.get('token')\n"
                            "    self._state = deepcopy(admission.get('session', {}).get('state', {}))\n"
                            '    self._records, self._final = ([], None)\n'
                            '    self._closed = False',
 'DurableSession.start': 'def start(self, message):\n'
                         '    events = _persist(self.store.events, self.context.actor, self.context.session_id)\n'
                         '    if _pending_tools(events):\n'
                         '        raise _unknown_outcome()\n'
                         '    self._records = []\n'
                         '    for event in events:\n'
                         "        if event['kind'] in {'message', 'assistant'}:\n"
                         "            self._records.append(deepcopy(event['payload']))\n"
                         "        elif event['kind'] == 'turn_completed' and event['payload'].get('status') == "
                         "'error':\n"
                         "            self._records.append({'role': 'assistant', 'source': 'runtime', 'content': "
                         "{'runtime_status': 'failed', 'code': event['payload']['stop_reason']}})\n"
                         "        elif event['kind'] == 'interrupted':\n"
                         "            self._records.append({'role': 'assistant', 'source': 'runtime', 'content': "
                         "{'runtime_status': 'interrupted'}})\n"
                         "    self.append([{'role': 'user', 'content': deepcopy(message)}])",
 'DurableSession.messages': 'def messages(self):\n    return deepcopy(self._records)',
 'DurableSession.append': 'def append(self, records):\n'
                          '    if self._closed or self._token is None:\n'
                          "        raise RuntimeError('SESSION_CLOSED')\n"
                          '    for record in records:\n'
                          '        _check_execution()\n'
                          '        copied = deepcopy(record)\n'
                          "        if copied.get('role') == 'assistant' and 'tool_call' not in copied:\n"
                          '            self._final = copied\n'
                          '        else:\n'
                          '            _persist(self.store.append, self.context.actor, self.context.session_id, '
                          "self._token, 'message', copied, self.context.turn_id, 'agent')\n"
                          '        self._records.append(copied)',
 'DurableSession.finish': 'def finish(self, result):\n'
                          '    if self._closed:\n'
                          "        raise RuntimeError('SESSION_CLOSED')\n"
                          "    if 'cached' in self.start_result:\n"
                          '        self._closed = True\n'
                          "        return deepcopy(self.start_result['cached'])\n"
                          '    _check_execution()\n'
                          "    state = {**self._state, 'turn_number': self._state['turn_number'] + 1}\n"
                          '    response = _persist(self.store.finish, self.context.actor, '
                          "self.context.session_id, self._token, state, {'status': 'complete', 'result': "
                          'deepcopy(result)}, assistant_event=self._final)\n'
                          '    self._closed = True\n'
                          '    self._records.clear()\n'
                          '    return response',
 'DurableSession.fail': 'def fail(self, error):\n'
                        '    if self._closed or self._token is None:\n'
                        '        return\n'
                        '    response = _persist(self.store.fail, self.context.actor, self.context.session_id, '
                        "self._token, self._state, {'status': 'error', 'stop_reason': getattr(error, 'code', "
                        'type(error).__name__)})\n'
                        '    self._closed = True\n'
                        '    self._records.clear()\n'
                        '    return response'}


def durable_session_source_violations(source):
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for owner in tree.body:
        if isinstance(owner, ast.ClassDef) and owner.name in {'DurableSessions', 'DurableSession'}:
            functions.update({owner.name + '.' + node.name: node for node in owner.body
                              if isinstance(node, ast.FunctionDef)})
    errors = []
    for name, expected in REVIEWED_FUNCTIONS.items():
        node = functions.get(name)
        if node is None or ast.dump(node, include_attributes=False) != ast.dump(
                ast.parse(expected).body[0], include_attributes=False):
            errors.append('durable session ' + name + ': reviewed admission/unknown/commit-acknowledgement boundary changed')
    return errors


WITNESSES = {
    'RecoveredCommit': 's.phase = "closed" /\\ s.mode = "replay" /\\ s.public = "success" /\\ s.persistenceError /\\ s.effects = s.replayEffects',
    'UnknownToolReplay': 's.phase = "closed" /\\ s.mode = "replay" /\\ s.pending /\\ s.effects = 1 /\\ s.public = "error" /\\ s.effects = s.replayEffects',
    'Success': 's.phase = "closed" /\\ s.mode = "fresh" /\\ s.public = "success" /\\ s.final /\\ s.turn = 1',
    'CommitUnknown': 's.phase = "uncertain" /\\ s.status = "complete" /\\ s.result = "success" /\\ ~s.closed /\\ s.public = "unknown"',
    'Replay': 's.phase = "closed" /\\ s.mode = "replay" /\\ s.public = "success" /\\ s.effects = s.replayEffects',
    'UnknownToolBlocked': 's.phase = "blocked" /\\ s.pending /\\ s.effects = 1 /\\ s.result = "error"',
    'ToolRecordUnknown': 's.phase = "closed" /\\ s.pending /\\ s.effects = 0 /\\ s.public = "unknown" /\\ s.status = "complete"',
    'CancellationRejected': 's.phase = "closed" /\\ s.cancelled /\\ s.result = "error" /\\ ~s.final',
}


def durable_session_violations(root: Path, registry: dict) -> list[str]:
    module = 'formal/session_catalog/DurableAdapter.tla'
    models = [row for machine in registry['machines'] for row in machine['models'] if row['module'] == module]
    if not (root / module).is_file() and not models:
        return []
    errors = durable_session_source_violations((root / 'kb_platform/harness_sessions.py').read_text('utf-8'))
    text = ' '.join((root / module).read_text('utf-8').split())
    for name, predicate in WITNESSES.items():
        invariant = 'No' + name + 'Witness'
        if f'{invariant} == ~({predicate})' not in text:
            errors.append('durable session terminal witness changed: ' + invariant)
        config = 'formal/session_catalog/DurableAdapter' + name + 'Witness.cfg'
        rows = [row for machine in registry['machines'] for row in machine['negative_controls']
                if row['module'] == module and row['config'] == config and row.get('expected_invariant') == invariant]
        if len(rows) != 1:
            errors.append('durable session requires registered witness: ' + invariant)
        path = root / config
        expected = ('SPECIFICATION Spec CONSTANTS CheckNamespace = TRUE CloseAfterAck = TRUE '
                    'PreserveCommitted = TRUE ReplayWithoutEffects = TRUE BlockUnknown = TRUE INVARIANTS '
                    + invariant + ' CHECK_DEADLOCK FALSE')
        if not path.is_file() or ' '.join(path.read_text('utf-8').split()) != expected:
            errors.append('durable session witness must preserve positive-model behavior: ' + invariant)
    return errors

import ast


COMMIT = """
scope = authority['scope_id']
with self.store.database.transaction(scope={'tenant_id': actor.tenant_id, 'kb_id': scope}) as transaction:
    current = locked_authority(KnowledgeTransaction(transaction, actor, scope, True), actor, scope)
    if current != authority:
        raise PlatformError('SCOPE_CHANGED', 'Authority changed before answer commit', 409)
    return self.store.finish_in_transaction(transaction, actor, session_id, token, state, response,
        expected_scope=scope, assistant_event=assistant_event)
"""
SCOPE = """
session = self._owner(db, actor, session_id)
if expected_scope is not None and session['scope'] != expected_scope:
    raise PlatformError('SCOPE_CHANGED', 'Session scope differs from verified authority', 409)
"""


def _method(root, file, owner, method):
    tree = ast.parse((root / file).read_text('utf-8'))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner]
    methods = [node for cls in classes for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method]
    return methods[0] if len(classes) == len(methods) == 1 and not methods[0].decorator_list else None


def _statements(nodes):
    return [ast.dump(node, include_attributes=False) for node in nodes]


def authority_commit_violations(root, registry):
    if not any(machine['id'] == 'knowledge_atomic_commit' for machine in registry['machines']):
        return []
    errors = []
    method = _method(root, 'kb_platform/knowledge_sessions.py', 'KnowledgeSessions', 'finish')
    if method is None or len(method.body) != 3 or _statements(method.body[1:]) != _statements(ast.parse(COMMIT).body):
        errors.append('authority commit must lock, compare the full snapshot and finish inside the same transaction')
    method = _method(root, 'kb_platform/session_store.py', 'SessionStore', 'finish_in_transaction')
    if method is None or _statements(method.body[:2]) != _statements(ast.parse(SCOPE).body):
        errors.append('authority commit must bind the persisted session owner and scope before staging effects')
    if method is not None and any(isinstance(node, (ast.With, ast.Try)) for node in ast.walk(method)):
        errors.append('authority commit effects cannot open a separate transaction or swallow storage failure')
    turn = _method(root, 'kb_platform/knowledge_turn.py', 'KnowledgeTurn', 'run')
    calls = [] if turn is None else [node for node in ast.walk(turn) if isinstance(node, ast.Call)
        and ast.unparse(node.func) == 'self.service.store.finish']
    if len(calls) != 1 or not any(keyword.arg == 'authority' and ast.unparse(keyword.value) == 'self.authority'
            for keyword in calls[0].keywords):
        errors.append('authority commit requires the original checked snapshot at the public finish boundary')
    handlers = [] if turn is None else [node for node in turn.body if isinstance(node, ast.Try)]
    expected = ast.parse("code = getattr(exc, 'code', type(exc).__name__)\nif code == 'COMMIT_UNKNOWN':\n    raise").body
    if len(handlers) != 1 or _statements(handlers[0].handlers[0].body[:2]) != _statements(expected):
        errors.append('authority commit unknown outcome must escape without failure publication or retry')
    model = root / 'formal/session_authority/AtomicAuthorityCommit.tla'
    text = ' '.join(model.read_text('utf-8').split())
    for guard in ('/\\ (~locked \\/ ~HoldLock)',
                  'allowed /\\ revision = initialRevision /\\ manifest = initialManifest',
                  'committedAllowed /\\ committedRevision = initialRevision /\\ committedManifest = initialManifest'):
        if guard not in text:
            errors.append('authority commit model lost a reviewed lock/snapshot predicate')
    return errors

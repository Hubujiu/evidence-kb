import ast
from pathlib import Path


REVIEWED_STATEMENTS = {'canonical': ["return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))"],
 'digest': ['return hashlib.sha256(canonical(value).encode()).hexdigest()'],
 '_owner': ['statement = select(sessions).where(sessions.c.id == session_id, sessions.c.tenant == '
            'actor.tenant_id, sessions.c.principal == actor.principal_id)',
            'row = self._row(db.execute(statement.with_for_update() if lock else statement).fetchone())',
            "if row is None:\n    raise PlatformError('SESSION_NOT_FOUND', 'Conversation not available', 404)",
            'return row'],
 'create': ["if not isinstance(initial_state, dict):\n    raise ValueError('INVALID_SESSION_STATE')",
            'session_id = uuid4().hex',
            'state = initial_state',
            'timestamp = now()',
            'with self._db() as db:\n'
            "    db.insert(sessions, {'id': session_id, 'tenant': actor.tenant_id, 'principal': "
            "actor.principal_id, 'scope': scope_id, 'revision': 0, 'state': canonical(state), 'created': "
            "timestamp, 'updated': timestamp})",
            'return self.load(actor, session_id)'],
 'load': ['with self._db(write=False) as db:\n'
          '    return self._decode(self._owner(db, actor, session_id, lock=False))'],
 'list_sessions': ['if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or (offset < '
                   '0):\n'
                   "    raise PlatformError('INVALID_SESSION_PAGE', 'Expected limit1..100 and nonnegative "
                   "offset', 422)",
                   'with self._db(write=False) as db:\n'
                   "    return [{'session_id': row['id'], 'scope_id': row['scope'], 'revision': row['revision'], "
                   "'created_at': row['created'], 'updated_at': row['updated']} for row in map(self._row, "
                   'db.execute(select(sessions.c.id, sessions.c.scope, sessions.c.revision, sessions.c.created, '
                   'sessions.c.updated).where(sessions.c.tenant == actor.tenant_id, sessions.c.principal == '
                   'actor.principal_id).order_by(sessions.c.updated.desc(), '
                   'sessions.c.id.desc()).limit(limit).offset(offset)).fetchall())]'],
 'events': ['with self._db(write=False) as db:\n'
            '    self._owner(db, actor, session_id, lock=False)\n'
            "    return [{**dict(row), 'payload': json.loads(row['payload'])} for row in map(self._row, "
            'db.execute(select(events).where(events.c.session == '
            'session_id).order_by(events.c.seq)).fetchall())]'],
 'artifact': ['key = digest(body)',
              'with self._db() as db:\n'
              '    self._owner(db, actor, session_id)\n'
              '    self._lease(db, session_id, token)\n'
              '    previous = db.execute(select(artifacts.c.body).where(artifacts.c.session == session_id, '
              'artifacts.c.id == key)).fetchone()\n'
              '    if previous is None:\n'
              "        db.insert(artifacts, {'session': session_id, 'id': key, 'body': canonical(body), "
              "'created': now()})\n"
              "    elif digest(json.loads(previous['body'])) != key:\n"
              "        raise PlatformError('ARTIFACT_INTEGRITY', 'Artifact integrity check failed', 503)",
              "return {'artifact_id': key, 'sha256': key, 'bytes': len(canonical(body).encode())}"],
 'read_artifact': ['with self._db(write=False) as db:\n'
                   '    self._owner(db, actor, session_id, lock=False)\n'
                   '    row = db.execute(select(artifacts.c.body).where(artifacts.c.session == session_id, '
                   'artifacts.c.id == artifact_id)).fetchone()\n'
                   '    if row is None:\n'
                   "        raise PlatformError('ARTIFACT_NOT_FOUND', 'Artifact not available', 404)\n"
                   "    body = json.loads(row['body'])\n"
                   '    if digest(body) != artifact_id:\n'
                   "        raise PlatformError('ARTIFACT_INTEGRITY', 'Artifact integrity check failed', 503)\n"
                   '    return body']}


def session_catalog_source_violations(source):
    tree = ast.parse(source)
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SessionStore']
    if len(owners) != 1:
        return ['session catalog owner missing or ambiguous']
    methods = {node.name: node for node in tree.body + owners[0].body if isinstance(node, ast.FunctionDef)}
    errors = []
    owner = methods.get('_owner')
    if (owner is None or [item.arg for item in owner.args.kwonlyargs] != ['lock']
            or [ast.unparse(item) for item in owner.args.kw_defaults] != ['True']):
        errors.append('session catalog requires default locked owner admission')
    for name, expected in REVIEWED_STATEMENTS.items():
        method = methods.get(name)
        if method is None or len(method.body) != len(expected) or any(
                ast.dump(node, include_attributes=False) != ast.dump(ast.parse(wanted).body[0], include_attributes=False)
                for node, wanted in zip(method.body, expected, strict=True)):
            errors.append('session catalog ' + name + ': reviewed owner/content/transaction boundary changed')
    return errors


WITNESSES = {
    'CreateUnknown': 'phase = "idle" /\\ createUnknown # {} /\\ createUnknown \\subseteq catalog',
    'ArtifactDedup': 'dedupSeen',
    'AuthorizedRead': 'observed.ok /\\ observed.artifact /\\ observed.authorized /\\ observed.content /\\ observed.bound /\\ catalog # {}',
}


def session_catalog_violations(root: Path, registry: dict) -> list[str]:
    module = 'formal/session_catalog/SessionCatalog.tla'
    models = [row for machine in registry['machines'] for row in machine['models'] if row['module'] == module]
    if not (root / module).is_file() and not models:
        return []
    errors = session_catalog_source_violations((root / 'kb_platform/session_store.py').read_text('utf-8'))
    text = ' '.join((root / module).read_text('utf-8').split())
    if "dedupSeen' = (dedupSeen \\/ stored[key] = \"good\")" not in text:
        errors.append('session catalog requires the grouped complete dedup Boolean RHS')
    for name, predicate in WITNESSES.items():
        invariant = 'No' + name + 'Witness'
        if f'{invariant} == ~({predicate})' not in text and not (name == 'ArtifactDedup' and f'{invariant} == ~dedupSeen' in text):
            errors.append('session catalog terminal witness changed: ' + invariant)
        config = 'formal/session_catalog/SessionCatalog' + name + 'Witness.cfg'
        rows = [row for machine in registry['machines'] for row in machine['negative_controls']
                if row['module'] == module and row['config'] == config and row.get('expected_invariant') == invariant]
        if len(rows) != 1:
            errors.append('session catalog requires registered witness: ' + invariant)
        path = root / config
        expected = ('SPECIFICATION Spec CONSTANTS CheckTenant = TRUE CheckPrincipal = TRUE '
                    'CheckContent = TRUE CheckSessionKey = TRUE CheckLease = TRUE INVARIANTS ' + invariant
                    + ' CHECK_DEADLOCK FALSE')
        if not path.is_file() or ' '.join(path.read_text('utf-8').split()) != expected:
            errors.append('session catalog witness must use the unchanged positive behavior: ' + invariant)
    return errors

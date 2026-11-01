from ..database_contracts import Migration, SchemaPackage


def lock_suffix(engine):
    return '' if engine == 'sqlite' else ' FOR UPDATE'


def session_schema(engine, owner):
    if engine not in {'sqlite', 'postgresql', 'mysql'}:
        raise ValueError('SESSION_DATABASE_UNSUPPORTED')
    identity = 'VARBINARY(1024)' if engine == 'mysql' else 'TEXT'
    short = 'VARBINARY(64)' if engine == 'mysql' else 'TEXT'
    body = 'LONGTEXT' if engine == 'mysql' else 'TEXT'
    real = 'DOUBLE PRECISION'
    statements = [
        f'''CREATE TABLE sessions (
            id {short} PRIMARY KEY, tenant {identity} NOT NULL, principal {identity} NOT NULL,
            scope {body} NOT NULL, revision INTEGER NOT NULL, state {body} NOT NULL,
            created VARCHAR(40) NOT NULL, updated VARCHAR(40) NOT NULL)''',
        f'''CREATE TABLE events (
            session {short} NOT NULL, seq INTEGER NOT NULL, kind {body} NOT NULL,
            payload {body} NOT NULL, group_id {body} NOT NULL, agent {body} NOT NULL,
            created VARCHAR(40) NOT NULL, PRIMARY KEY(session,seq),
            FOREIGN KEY(session) REFERENCES sessions(id))''',
        f'''CREATE TABLE requests (
            session {short} NOT NULL, request_id {identity} NOT NULL, fingerprint {identity} NOT NULL,
            token {short} NOT NULL, status VARCHAR(20) NOT NULL, expires {real} NOT NULL,
            response {body}, PRIMARY KEY(session,request_id),
            FOREIGN KEY(session) REFERENCES sessions(id))''',
        f'''CREATE TABLE artifacts (
            session {short} NOT NULL, id {short} NOT NULL, body {body} NOT NULL,
            created VARCHAR(40) NOT NULL, PRIMARY KEY(session,id),
            FOREIGN KEY(session) REFERENCES sessions(id))''',
    ]
    if engine == 'postgresql':
        statements.append('''CREATE FUNCTION session_reject_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable session record'; END; $$''')
    for table in ('events', 'artifacts'):
        for operation in ('UPDATE', 'DELETE'):
            name = f'immutable_{table}_{operation.lower()}'
            if engine == 'sqlite':
                statement = (f'CREATE TRIGGER {name} BEFORE {operation} ON {table} '
                             "BEGIN SELECT RAISE(ABORT,'immutable session record'); END")
            elif engine == 'postgresql':
                statement = (f'CREATE TRIGGER {name} BEFORE {operation} ON {table} '
                             'FOR EACH ROW EXECUTE FUNCTION session_reject_mutation()')
            else:
                statement = (f'CREATE TRIGGER {name} BEFORE {operation} ON {table} '
                             "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='immutable session record'")
            statements.append(statement)
    return SchemaPackage(owner, (Migration(1, tuple(statements)),), runtime_appendonly=('events', 'artifacts'))


from contextlib import closing
from pathlib import Path
import json
import sqlite3

from .session_store import SessionStore, canonical, digest


TABLE_COLUMNS = {
    'sessions': ('id', 'tenant', 'principal', 'scope', 'revision', 'state', 'created', 'updated'),
    'events': ('session', 'seq', 'kind', 'payload', 'group_id', 'agent', 'created'),
    'requests': ('session', 'request_id', 'fingerprint', 'token', 'status', 'expires', 'response'),
    'artifacts': ('session', 'id', 'body', 'created'),
}
TABLE_KEYS = {'sessions': ('id',), 'events': ('session', 'seq'),
              'requests': ('session', 'request_id'), 'artifacts': ('session', 'id')}


def _ordered(table, rows):
    return sorted(rows, key=lambda row: tuple(row[key] for key in TABLE_KEYS[table]))


def _package(tables, source):
    tables = {table: _ordered(table, tables[table]) for table in TABLE_COLUMNS}
    body = {'version': 1, 'source': source, 'tables': tables,
            'counts': {table: len(rows) for table, rows in tables.items()},
            'record_hashes': {table: [digest(row) for row in rows] for table, rows in tables.items()}}
    result = {**body, 'sha256': digest(body)}
    validate_export(result)
    return result


def validate_export(package):
    if not isinstance(package, dict) or package.get('version') != 1:
        raise ValueError('SESSION_EXPORT_VERSION')
    body = {key: value for key, value in package.items() if key != 'sha256'}
    if digest(body) != package.get('sha256'):
        raise ValueError('SESSION_EXPORT_INTEGRITY')
    tables = package.get('tables', {})
    if set(tables) != set(TABLE_COLUMNS):
        raise ValueError('SESSION_EXPORT_TABLES')
    for table, columns in TABLE_COLUMNS.items():
        rows = tables[table]
        if (not isinstance(rows, list) or any(set(row) != set(columns) for row in rows)
                or rows != _ordered(table, rows)
                or len({tuple(row[key] for key in TABLE_KEYS[table]) for row in rows}) != len(rows)
                or package['counts'].get(table) != len(rows)
                or package['record_hashes'].get(table) != [digest(row) for row in rows]):
            raise ValueError('SESSION_EXPORT_RECORDS')
    sessions = {row['id']: row for row in tables['sessions']}
    events = {sid: [] for sid in sessions}
    for row in tables['sessions']:
        if (not isinstance(json.loads(row['state']), dict)
                or type(row['revision']) is not int or row['revision'] < 0):
            raise ValueError('SESSION_EXPORT_STATE')
    for table in ('events', 'requests', 'artifacts'):
        if any(row['session'] not in sessions for row in tables[table]):
            raise ValueError('SESSION_EXPORT_ORPHAN')
    for event in tables['events']:
        json.loads(event['payload'])
        events[event['session']].append(event)
    for sid, records in events.items():
        if ([row['seq'] for row in records] != list(range(1, len(records) + 1))
                or sum(row['kind'] == 'turn_completed' for row in records) != sessions[sid]['revision']):
            raise ValueError('SESSION_EXPORT_REVISION')
    for row in tables['artifacts']:
        if digest(json.loads(row['body'])) != row['id']:
            raise ValueError('SESSION_EXPORT_ARTIFACT_INTEGRITY')
    tokens = set()
    for request in tables['requests']:
        key = request['session'], request['token']
        if request['status'] not in {'complete', 'running', 'interrupted'} or key in tokens:
            raise ValueError('SESSION_EXPORT_REQUEST')
        tokens.add(key)
        if request['status'] == 'complete':
            response = json.loads(request['response'])
            completed = [event for event in events[request['session']]
                         if event['kind'] == 'turn_completed' and event['group_id'] == request['request_id']]
            if (len(completed) != 1 or json.loads(completed[0]['payload']) != response
                    or response.get('session_id') != request['session']
                    or not 1 <= response.get('revision', 0) <= sessions[request['session']]['revision']):
                raise ValueError('SESSION_EXPORT_RESPONSE')
    return package


def export_sessions(store, *, source_frozen=False):
    if source_frozen is not True:
        raise ValueError('SESSION_SOURCE_MUST_BE_FROZEN')
    with store.database.transaction(write=False) as tx:
        tables = {table: [store._row(row) for row in tx.execute(f'SELECT * FROM {table}').fetchall()]
                  for table in TABLE_COLUMNS}
    return _package(tables, {'kind': 'database', 'dataset_id': store.identity.dataset_id})


def export_legacy_sqlite(path, *, source_frozen=False):
    if source_frozen is not True:
        raise ValueError('SESSION_SOURCE_MUST_BE_FROZEN')
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        tables = {table: [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]
                  for table in TABLE_COLUMNS}
    return _package(tables, {'kind': 'legacy-sqlite', 'dataset_id': None})


def import_sessions(store, package, *, target_offline=False):
    if target_offline is not True:
        raise ValueError('SESSION_TARGET_MUST_BE_OFFLINE')
    validate_export(package)
    with store.database.transaction(write=True) as tx:
        if any(tx.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']
               for table in TABLE_COLUMNS):
            raise ValueError('SESSION_IMPORT_TARGET_NOT_EMPTY')
        for table, columns in TABLE_COLUMNS.items():
            sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(':' + key for key in columns)})"
            for row in package['tables'][table]:
                tx.execute(sql, row)
        for table in TABLE_COLUMNS:
            actual = _ordered(table, [store._row(row) for row in tx.execute(f'SELECT * FROM {table}').fetchall()])
            if actual != package['tables'][table] or [digest(row) for row in actual] != package['record_hashes'][table]:
                raise ValueError('SESSION_IMPORT_VERIFICATION_FAILED')
    return {'source_dataset_id': package['source']['dataset_id'],
            'target_dataset_id': store.identity.dataset_id, 'mode': 'clone',
            'counts': dict(package['counts']), 'sha256': package['sha256']}


def write_export(path, package):
    validate_export(package)
    with Path(path).open('x', encoding='utf-8') as output:
        output.write(canonical(package) + '\n')


def read_export(path):
    return validate_export(json.loads(Path(path).read_text(encoding='utf-8')))


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description='Offline session export/import with record verification')
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    sources = export.add_mutually_exclusive_group(required=True)
    sources.add_argument('--legacy-sqlite')
    sources.add_argument('--root')
    export.add_argument('--source-frozen', action='store_true', required=True)
    export.add_argument('--output', required=True)
    clone = commands.add_parser('import')
    clone.add_argument('--input', required=True)
    clone.add_argument('--root', required=True)
    clone.add_argument('--target-offline', action='store_true', required=True)
    args = parser.parse_args(argv)
    if args.command == 'export' and args.legacy_sqlite:
        package = export_legacy_sqlite(args.legacy_sqlite, source_frozen=args.source_frozen)
        write_export(args.output, package)
        result = {'counts': package['counts'], 'sha256': package['sha256']}
    else:
        store = SessionStore(args.root)
        try:
            if args.command == 'export':
                package = export_sessions(store, source_frozen=args.source_frozen)
                write_export(args.output, package)
                result = {'counts': package['counts'], 'sha256': package['sha256']}
            else:
                result = import_sessions(store, read_export(args.input), target_offline=args.target_offline)
        finally:
            store.close()
    print(canonical(result))


if __name__ == '__main__':
    main()

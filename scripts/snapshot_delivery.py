from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb_platform.rag_testing.contracts import digest
from kb_platform.rag_testing.runner import write_json

ROOT = Path(__file__).resolve().parents[1]


def checksum(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def private_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT / '.local'):
        raise ValueError('RECOVERY_PATH_MUST_BE_PRIVATE_WORKSPACE_LOCAL')
    return path


def require_quiescent(port=58089):
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(('127.0.0.1', port)) == 0:
            raise ValueError('STOP_OWNED_DELIVERY_SERVICE_BEFORE_SNAPSHOT')


def docker(args):
    result = subprocess.run(['docker', 'compose', *args], cwd=ROOT, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError('LOCAL_RECOVERY_DOCKER_COMMAND_FAILED')
    return result.stdout.decode('utf-8').strip()


def metadata_fingerprints(conn):
    tables = conn.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('public','kb_search') ORDER BY schemaname,tablename").fetchall()
    result = {}
    for schema, name in tables:
        query = sql.SQL("SELECT count(*),md5(coalesce(string_agg(row_hash,'' ORDER BY row_hash),'')) "
                        "FROM (SELECT md5(to_jsonb(t)::text) AS row_hash FROM {} t) q").format(sql.Identifier(schema, name))
        count, fingerprint = conn.execute(query).fetchone()
        result[name if schema == 'public' else schema + '.' + name] = {'rows': count, 'content_md5': fingerprint}
    return result


def sqlite_fingerprints(conn):
    result = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        rows = [digest([value.hex() if isinstance(value, bytes) else value for value in row])
                for row in conn.execute('SELECT * FROM ' + quoted)]
        result[name] = {'rows': len(rows), 'content_sha256': digest(sorted(rows))}
    return result


def snapshot_sqlite(source, target):
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
        original.execute('PRAGMA query_only=ON')
        original.execute('BEGIN')
        journal = original.execute('PRAGMA journal_mode').fetchone()[0]
        before = sqlite_fingerprints(original)
        if 'calls' in before and original.execute("SELECT count(*) FROM calls WHERE status IN ('STARTED','UNKNOWN')").fetchone()[0]:
            raise ValueError('UNRESOLVED_REQUESTS_PREVENT_RECOVERY_SNAPSHOT')
        with sqlite3.connect(target) as restored:
            original.backup(restored)
            if restored.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or sqlite_fingerprints(restored) != before:
                raise ValueError('SQLITE_SNAPSHOT_CONTENT_MISMATCH')
        if journal != 'wal':


            shutil.copyfile(source, target)
            if checksum(source) != checksum(target):
                raise ValueError('SQLITE_EXACT_COPY_CHANGED')
            with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as restored:
                if restored.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or sqlite_fingerprints(restored) != before:
                    raise ValueError('SQLITE_EXACT_COPY_CONTENT_MISMATCH')
    return before


def verify_linked_budgets(application, source_roots):

    from kb_platform.rag_testing.budget_batches import LinkedBudgetLedger, freeze_predecessor

    results = []
    for manifest_path in sorted(application.rglob('batch.json')):
        value = json.loads(manifest_path.read_text('utf-8'))
        if value.get('protocol') != 'delivery-runtime-budget-batch/1':
            continue
        relocated = []
        for parent in value['predecessors']:
            original = Path(parent['path']).resolve()
            matches = [(prefix, Path(root).resolve()) for prefix, root in source_roots.items()
                       if original.is_relative_to(Path(root).resolve())]
            if len(matches) != 1:
                raise ValueError('RECOVERY_BUDGET_PARENT_OUTSIDE_PACK')
            prefix, source = matches[0]
            target = application / prefix / original.relative_to(source)
            current = freeze_predecessor(target, parent['database_sha256'])
            if current['snapshot_sha256'] != parent['snapshot_sha256']:
                raise ValueError('RECOVERY_BUDGET_PARENT_LOGICAL_IDENTITY_CHANGED')
            relocated.append({**parent, 'path': str(target)})
        ledger = LinkedBudgetLedger(manifest_path.parent / 'physical-calls.sqlite3',
                                    value['caps'], predecessors=relocated)
        cumulative = ledger.cumulative_summary()
        if cumulative['status'] != 'SETTLED':
            raise ValueError('RECOVERED_LINKED_BUDGET_NOT_SETTLED')
        results.append({'batch_id': value['id'], 'parent_binary_and_logical_identities_verified': len(relocated),
            'cumulative': cumulative['cumulative'], 'physical_request_ids_and_old_fees_preserved': True,
            'configuration_paths_relocated_in_memory_only': True, 'new_requests': 0})
    return results


def read_index(client, name):
    mapping = client.get(f'/{name}/_mapping').raise_for_status().json()[name]['mappings']
    settings = client.get(f'/{name}/_settings').raise_for_status().json()[name]['settings']['index']
    response = client.post(f'/{name}/_search', params={'scroll': '2m'},
        json={'size': 500, 'query': {'match_all': {}}, 'sort': ['_doc']}).raise_for_status().json()
    scroll, documents = response.get('_scroll_id'), []
    try:
        while response['hits']['hits']:
            documents.extend({'id': row['_id'], 'source': row['_source']} for row in response['hits']['hits'])
            response = client.post('/_search/scroll', json={'scroll': '2m', 'scroll_id': scroll}).raise_for_status().json()
            scroll = response.get('_scroll_id', scroll)
    finally:
        if scroll:
            client.request('DELETE', '/_search/scroll', json={'scroll_id': [scroll]}).raise_for_status()
    return {'index': name, 'mapping': mapping, 'settings': settings,
        'documents': sorted(documents, key=lambda row: row['id']),
        'document_sha256': digest(sorted(documents, key=lambda row: row['id']))}


def snapshot_application(run_root, output, include_roots=()):

    roots = [(Path('.'), private_path(run_root))]
    roots.extend((Path('_linked_budgets') / str(number), private_path(path))
                 for number, path in enumerate(include_roots))
    output = private_path(output)
    paths = [source for _, source in roots]
    for number, path in enumerate(paths):
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError('RECOVERY_OUTPUT_MUST_NOT_OVERLAP_SOURCE')
        if any(path == other or path.is_relative_to(other) or other.is_relative_to(path)
               for other in paths[:number]):
            raise ValueError('RECOVERY_SOURCE_ROOTS_MUST_NOT_OVERLAP')
    if any(not path.is_dir() for path in paths):
        raise ValueError('RECOVERY_SOURCE_DIRECTORY_MISSING')
    if (paths[0] / '_linked_budgets').exists():
        raise ValueError('RECOVERY_RESERVED_DESTINATION_COLLISION')
    files, sqlite = {}, {}
    for prefix, source_root in roots:
        for source in sorted(source_root.rglob('*')):
            if source.is_symlink():
                raise ValueError('RECOVERY_SYMLINK_NOT_ALLOWED')
            if not source.is_file() or source.name.endswith(('-wal', '-shm')):
                continue
            relative = prefix / source.relative_to(source_root)
            target = output / 'application' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix in ('.sqlite3', '.db'):
                sqlite[relative.as_posix()] = snapshot_sqlite(source, target)
            else:
                shutil.copyfile(source, target)
                if checksum(source) != checksum(target):
                    raise ValueError('APPLICATION_FILE_CHANGED_DURING_SNAPSHOT')
            files[relative.as_posix()] = checksum(target)
    return files, sqlite, {prefix.as_posix(): str(path) for prefix, path in roots}


def create(run_root, output, admin_dsn, search_url, prepared, include_roots=(), *, search_backend='postgres'):
    run_root, output = private_path(run_root), private_path(output)
    if output == run_root or output.is_relative_to(run_root) or run_root.is_relative_to(output):
        raise ValueError('RECOVERY_OUTPUT_MUST_NOT_OVERLAP_SOURCE')
    require_quiescent()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'state.json', {'status': 'STARTED'})
    files, sqlite, source_roots = snapshot_application(run_root, output, include_roots)
    selected = json.loads(Path(prepared).read_text('utf-8'))
    tenant = selected['identity']['tenant_id']
    scopes = [value['scope_id'] for value in selected['splits'].values()]
    dump_name = 'kb-recovery-' + uuid4().hex + '.dump'
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL TIME ZONE 'UTC'")
        snapshot = conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
        fingerprints = metadata_fingerprints(conn)
        indexes = [row[0] for row in conn.execute(
            "SELECT b.index_name FROM builds b JOIN knowledge_bases k USING (tenant_id,kb_id) "
            "WHERE b.build_id=k.manifest_id AND "
            "b.tenant_id=%s AND b.kb_id=ANY(%s) AND b.status='PUBLISHED' ORDER BY b.index_name",
            (tenant, scopes))]
        docker(['exec', '-T', 'postgres', 'pg_dump', '-U', 'kb_admin', '-d', 'evidence_kb', '-Fc',
                '--snapshot', snapshot, '-f', '/tmp/' + dump_name])
        docker(['cp', 'postgres:/tmp/' + dump_name, str(output / 'metadata.dump')])
        conn.execute('ROLLBACK')
    if len(indexes) != len(scopes):
        raise ValueError('RECOVERY_PUBLISHED_INDEX_SET_INCOMPLETE')
    search = {}
    with httpx.Client(base_url=search_url, timeout=60, trust_env=False) as client:
        for index in indexes:
            if not re.fullmatch(r'kb-[0-9a-f]{24}-[0-9a-f]{32}', index):
                raise ValueError('UNEXPECTED_RECOVERY_INDEX_NAME')
            if search_backend == 'postgres':
                from kb_platform.postgres_search import PostgresSearch
                backend = PostgresSearch(admin_dsn)
                index_manifest = backend.verify(index)
                documents = [dict(id=u['unit_id'], source=u) for u in backend.read_units(index)]
                data = dict(index=index, backend='postgres', dimension=index_manifest['dimension'],
                            documents=documents, document_sha256=digest(documents))
            elif search_backend == 'embedded':
                from kb_platform.embedded_search import EmbeddedSearch
                backend = EmbeddedSearch(run_root / 'originals' / '.search-indexes')
                index_manifest = backend.verify(index)
                documents = [dict(id=u['unit_id'], source=u) for u in backend.read_units(index)]
                data = dict(index=index, backend='embedded', dimension=index_manifest['dimension'],
                            documents=documents, document_sha256=digest(documents))
            else:
                data = read_index(client, index)
            path = output / (index + '.json')
            write_json(path, data)
            search[index] = {'file': path.name, 'sha256': checksum(path),
                'documents': len(data['documents']), 'document_sha256': data['document_sha256']}
    manifest = {'protocol': 'private-delivery-recovery/1', 'source_root': str(run_root),
        'tenant_id': tenant, 'scope_ids': scopes, 'files': files, 'sqlite': sqlite,
        'source_roots': source_roots,
        'metadata_dump_sha256': checksum(output / 'metadata.dump'), 'metadata_tables': fingerprints,
        'search_indexes': search, 'new_model_embedding_calls': 0,
        'private_credentials_in_application_copy': True,
        'scope': 'Delivery application files and selected published indexes; PostgreSQL shared metadata is fully dumped.',
        'limitations': ['No automatic live failover; restored configuration absolute paths must be reviewed.',
            'Other tenants original stores/indexes are outside this delivery recovery pack.']}
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'state.json', {'status': 'COMPLETE', 'manifest_sha256': checksum(output / 'manifest.json')})
    return {'status': 'COMPLETE', 'application_files': len(files), 'sqlite_databases': len(sqlite),
        'published_indexes': len(search), 'new_model_embedding_calls': 0}


def verify(backup, output, admin_dsn, search_url):
    backup, output = private_path(backup), private_path(output)
    if output == backup or output.is_relative_to(backup) or backup.is_relative_to(output):
        raise ValueError('RESTORE_OUTPUT_MUST_NOT_OVERLAP_BACKUP')
    manifest = json.loads((backup / 'manifest.json').read_text('utf-8'))
    state = json.loads((backup / 'state.json').read_text('utf-8'))
    if state.get('status') != 'COMPLETE' or state['manifest_sha256'] != checksum(backup / 'manifest.json'):
        raise ValueError('RECOVERY_MANIFEST_UNSEALED')
    if checksum(backup / 'metadata.dump') != manifest['metadata_dump_sha256']:
        raise ValueError('RECOVERY_DUMP_CHECKSUM_MISMATCH')
    output.mkdir(parents=True, exist_ok=False)
    for relative, expected in manifest['files'].items():
        if Path(relative).is_absolute():
            raise ValueError('RECOVERY_ABSOLUTE_FILE_PATH_REJECTED')
        source = (backup / 'application' / relative).resolve()
        if not source.is_relative_to(backup / 'application') or checksum(source) != expected:
            raise ValueError('RECOVERY_FILE_CHECKSUM_MISMATCH')
        target = output / 'application' / relative
        if not target.resolve().is_relative_to(output / 'application'):
            raise ValueError('RECOVERY_TARGET_PATH_OUTSIDE_OUTPUT')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if relative in manifest['sqlite']:
            with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or sqlite_fingerprints(db) != manifest['sqlite'][relative]:
                    raise ValueError('RESTORED_SQLITE_CONTENT_MISMATCH')
    database = 'kb_delivery_verify_' + uuid4().hex[:16]
    dump_name = database + '.dump'
    docker(['exec', '-T', 'postgres', 'createdb', '-U', 'kb_admin', '-T', 'template0', database])
    docker(['cp', str(backup / 'metadata.dump'), 'postgres:/tmp/' + dump_name])
    docker(['exec', '-T', 'postgres', 'pg_restore', '-U', 'kb_admin', '-d', database,
            '--no-owner', '--exit-on-error', '/tmp/' + dump_name])
    with psycopg.connect(admin_dsn, dbname=database) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        restored = metadata_fingerprints(conn)
    if restored != manifest['metadata_tables']:
        raise ValueError('RESTORED_POSTGRES_CONTENT_MISMATCH')
    restored_indexes = []
    with httpx.Client(base_url=search_url, timeout=60, trust_env=False) as client:
        for original, info in manifest['search_indexes'].items():
            path = backup / info['file']
            if not path.resolve().is_relative_to(backup) or checksum(path) != info['sha256']:
                raise ValueError('RECOVERY_SEARCH_CHECKSUM_MISMATCH')
            data = json.loads(path.read_text('utf-8'))
            if data.get('backend') == 'postgres':
                from kb_platform.postgres_search import PostgresSearch
                backend = PostgresSearch(psycopg.conninfo.make_conninfo(admin_dsn, dbname=database))
                backend.verify(original)
                observed = [dict(id=u['unit_id'], source=u) for u in backend.read_units(original)]
                if digest(observed) != info['document_sha256']:
                    raise ValueError('RESTORED_SEARCH_CONTENT_MISMATCH')
                restored_indexes.append(dict(original=original, restored=original, documents=len(observed),
                    content_sha256=digest(observed), state_after_verification='POSTGRES_RESTORED'))
                continue
            if data.get('backend') == 'embedded':
                from kb_platform.embedded_search import EmbeddedSearch
                backend = EmbeddedSearch(output / 'verified-search-indexes')
                backend.stage(original, [r['source'] for r in data['documents']], data['dimension'])
                backend.verify(original)
                observed = [dict(id=u['unit_id'], source=u) for u in backend.read_units(original)]
                if digest(observed) != info['document_sha256']:
                    raise ValueError('RESTORED_SEARCH_CONTENT_MISMATCH')
                restored_indexes.append(dict(original=original, restored=original, documents=len(observed),
                    content_sha256=digest(observed), state_after_verification='IMMUTABLE_LOCAL'))
                continue
            index = 'kb-' + uuid4().hex[:24] + '-' + uuid4().hex
            settings = {key: value for key, value in data['settings'].items()
                        if key not in {'uuid', 'provided_name', 'creation_date', 'version', 'history_uuid', 'blocks'}}
            settings['number_of_replicas'] = 0
            client.put('/' + index, json={'settings': settings, 'mappings': data['mapping']}).raise_for_status()
            rows = []
            for row in data['documents']:
                rows.extend([json.dumps({'index': {'_index': index, '_id': row['id']}}),
                             json.dumps(row['source'], ensure_ascii=False)])
            if rows:
                bulk = client.post('/_bulk', content=('\n'.join(rows) + '\n').encode('utf-8'),
                    headers={'Content-Type': 'application/x-ndjson'}).raise_for_status().json()
                if bulk.get('errors'):
                    raise ValueError('RESTORED_SEARCH_BULK_FAILED')
            client.post('/' + index + '/_refresh').raise_for_status()
            observed = read_index(client, index)
            if observed['document_sha256'] != info['document_sha256']:
                raise ValueError('RESTORED_SEARCH_CONTENT_MISMATCH')
            client.post('/' + index + '/_close').raise_for_status()
            restored_indexes.append({'original': original, 'restored': index, 'documents': len(observed['documents']),
                'content_sha256': observed['document_sha256'], 'state_after_verification': 'CLOSED'})
    linked_budgets = verify_linked_budgets(output / 'application', manifest.get('source_roots', {'.': manifest['source_root']}))
    result = {'status': 'PASS', 'protocol': manifest['protocol'], 'restored_database': database,
        'metadata_tables_verified': len(restored), 'application_files_verified': len(manifest['files']),
        'sqlite_databases_verified': len(manifest['sqlite']), 'restored_indexes': restored_indexes,
        'live_resources_overwritten': 0, 'new_model_embedding_calls': 0,
        'scope': manifest['scope'], 'limitations': manifest['limitations'],
        'restored_linked_budget_admission': linked_budgets}
    write_json(output / 'verification.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Private local recovery pack and non-destructive restore verification.\n\nCreates PostgreSQL's exported-snapshot dump, exact published-index documents,\nand consistent SQLite/application files. Verification uses a new database, new\nsearch indexes and an empty directory. It never overwrites a live database,\nindex or directory, and never calls any model.\n")
    parser.add_argument('command', choices=('create', 'verify'))
    parser.add_argument('--run-root', type=Path, default=ROOT / '.local/delivery-20261026-01')
    parser.add_argument('--prepared', type=Path, default=ROOT / '.local/delivery-20261026-01/prepared-02/prepared.json')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--include-root', action='append', type=Path, default=[],
                        help='Explicit settled continuation/linked-budget directory to preserve')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--admin-dsn-env', default='KB_RECOVERY_ADMIN_DSN')
    parser.add_argument('--search-url', default='http://127.0.0.1:59200')
    parser.add_argument('--search-backend', choices=('postgres', 'embedded', 'opensearch'), default='postgres')
    args = parser.parse_args()
    import os

    admin = os.environ.get(args.admin_dsn_env)
    if not admin:
        parser.error('Set the explicit local recovery admin DSN environment; never pass credentials as arguments')
    result = create(args.run_root, args.output, admin, args.search_url, args.prepared, args.include_root,
                    search_backend=args.search_backend) if args.command == 'create' else verify(
        args.backup, args.output, admin, args.search_url)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

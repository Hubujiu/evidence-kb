"""Import immutable local generations into a restored candidate PostgreSQL database.

Does not change authority pointers or call models. Semantic indexes can be copied,
but their old published manifests remain read-only: current dependency-bound
semantic manifests must be rebuilt separately from verified original capsules.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb_platform.embedded_search import EmbeddedSearch  # noqa: E402
from kb_platform.postgres_search import PostgresSearch  # noqa: E402
from kb_platform.search import content_order  # noqa: E402
from kb_platform.rag_testing.contracts import digest  # noqa: E402
from kb_platform.rag_testing.runner import write_json  # noqa: E402


def published_builds(dsn, tenants):
    result = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for tenant in sorted(set(tenants)):
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (tenant,))
            scopes = conn.execute('SELECT tenant_id,kb_id,manifest_id FROM knowledge_bases '
                                 'WHERE tenant_id=%s ORDER BY kb_id', (tenant,)).fetchall()
            for scope in scopes:
                if scope['manifest_id'] is None:
                    continue
                conn.execute("SELECT set_config('app.kb_id',%s,true)", (scope['kb_id'],))
                row = conn.execute('SELECT tenant_id,kb_id,build_id,index_name,status,unit_count FROM builds '
                                   'WHERE tenant_id=%s AND kb_id=%s AND build_id=%s',
                                   (tenant, scope['kb_id'], scope['manifest_id'])).fetchone()
                if not row or row['status'] != 'PUBLISHED':
                    raise ValueError('PUBLISHED_BUILD_MISSING_OR_INVALID:' + scope['kb_id'])
                result.append(row)
    return result


def candidate_exists(dsn, index, tenant, kb):
    with psycopg.connect(dsn) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.kb_id',%s,true)", (tenant, kb))
        return conn.execute('SELECT 1 FROM search_generations WHERE index_name=%s', (index,)).fetchone() is not None


def import_index(dsn, target, roots, item):
    index = item['index_name']
    found = [source for source in roots if source._path(index).is_dir()]
    if not found:
        raise ValueError('LOCAL_INDEX_MISSING:' + index)
    units, manifest, root = None, None, None
    for source in found:
        current = source.verify(index)
        observed = source.read_units(index)
        if units is not None and (observed != units or current != manifest):
            raise ValueError('AMBIGUOUS_LOCAL_INDEX:' + index)
        units, manifest, root = observed, current, source.root
    if not units or len(units) != manifest['count']:
        raise ValueError('LOCAL_INDEX_COUNT_MISMATCH:' + index)
    if 'unit_count' in item and len(units) != item['unit_count']:
        raise ValueError('AUTHORITY_INDEX_COUNT_MISMATCH:' + index)
    if any((unit['tenant_id'], unit['kb_id'], unit['build_id']) !=
           (item['tenant_id'], item['kb_id'], item['build_id']) for unit in units):
        raise ValueError('AUTHORITY_INDEX_IDENTITY_MISMATCH:' + index)
    existed = candidate_exists(dsn, index, item['tenant_id'], item['kb_id'])
    if not existed:
        target.stage(index, units, vector_dimension=manifest['dimension'])
    target.verify(index)
    observed = target.read_units(index)
    if observed != sorted(units, key=content_order):
        raise ValueError('EXACT_INDEX_READBACK_MISMATCH:' + index)
    # Verify source integrity again: importing must not accept a changed local snapshot.
    for source in found:
        if source.verify(index) != manifest:
            raise ValueError('LOCAL_INDEX_CHANGED_DURING_IMPORT:' + index)
    return {'index_name': index, 'units': len(units), 'dimension': manifest['dimension'],
            'source_index_root': str(root), 'source_manifest_sha256': digest(manifest),
            'units_sha256': digest(observed), 'exact_readback': True, 'already_present': existed}


def semantic_inventory(config_path, tenants):
    config = json.loads(config_path.read_text('utf-8'))
    root = Path(config['storage_root']).resolve()
    inventory = []
    for pointer_path in sorted(root.glob('*/active.json')):
        pointer = json.loads(pointer_path.read_text('utf-8'))
        name = pointer['manifest']
        if Path(name).name != name:
            raise ValueError('INVALID_SEMANTIC_MANIFEST_PATH')
        path = pointer_path.parent / name
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != pointer['sha256']:
            raise ValueError('SEMANTIC_MANIFEST_CHECKSUM_MISMATCH:' + str(path))
        manifest = json.loads(raw)
        if manifest['tenant_id'] in tenants:
            inventory.append({'tenant_id': manifest['tenant_id'], 'kb_id': manifest['kb_id'],
                              'build_id': manifest['build_id'], 'index_name': manifest['index_name'],
                              'original_manifest': str(path), 'original_manifest_sha256': pointer['sha256'],
                              'publication_status': 'NEEDS_CURRENT_DEPENDENCY_CAPSULE_REBUILD'})
    return inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-env', default='KB_RECOVERY_ADMIN_DSN')
    parser.add_argument('--storage-root', type=Path, required=True,
                        help='Originals storage root containing .search-indexes')
    parser.add_argument('--additional-index-root', type=Path, action='append', default=[],
                        help='Additional .search-indexes directory (not originals root)')
    parser.add_argument('--tenant', action='append', required=True)
    parser.add_argument('--semantic-runtime', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ[args.dsn_env]
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'ordinary_indexes': [], 'semantic_indexes': [], 'failures': [],
              'new_embedding_calls': 0, 'authority_changes': 0,
              'observation': 'Exact immutable units and original vector JSON; no semantic manifest substitution'}
    target = PostgresSearch(dsn)
    sources = [EmbeddedSearch(path) for path in dict.fromkeys(
        [args.storage_root.resolve() / '.search-indexes', *[p.resolve() for p in args.additional_index_root]])]
    try:
        before = published_builds(dsn, args.tenant)
        write_json(args.output / 'published-builds.json', before)
        report['published_builds_sha256'] = digest(before)
        for item in before:
            try:
                report['ordinary_indexes'].append(import_index(dsn, target, sources, item))
            except Exception as exc:
                report['failures'].append({'index_name': item['index_name'], 'phase': 'ordinary',
                                           'error': str(exc), 'type': type(exc).__name__})
        if args.semantic_runtime:
            for item in semantic_inventory(args.semantic_runtime, args.tenant):
                try:
                    result = import_index(dsn, target, sources, item)
                    report['semantic_indexes'].append({**item, **result})
                except Exception as exc:
                    report['failures'].append({'index_name': item['index_name'], 'phase': 'semantic',
                                               'error': str(exc), 'type': type(exc).__name__})
        if published_builds(dsn, args.tenant) != before:
            raise ValueError('AUTHORITY_POINTERS_CHANGED_DURING_IMPORT')
        report['authority_pointers_unchanged'] = True
        report['status'] = 'ERROR' if report['failures'] else 'IMPORTED'
        if report['semantic_indexes']:
            report['semantic_publication_complete'] = False
        write_json(args.output / 'receipt.json', report)
    except Exception as exc:
        report['status'] = 'ERROR'
        report['failures'].append({'phase': 'inventory_or_verification', 'error': str(exc), 'type': type(exc).__name__})
        write_json(args.output / 'receipt.json', report)
        raise
    finally:
        target.close()
        for source in sources:
            source.close()
    print(json.dumps({'status': report['status'], 'ordinary_indexes': len(report['ordinary_indexes']),
                      'semantic_indexes': len(report['semantic_indexes']), 'failures': len(report['failures']),
                      'new_embedding_calls': 0}))
    return 1 if report['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

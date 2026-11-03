from copy import deepcopy

from .knowledge_schema import LEGACY_METADATA, knowledge_schema
from .knowledge_transfer import _tables
from .session_transfer import validate_export as validate_sessions
from .storage_adapters.structured_transfer import content_hash, import_structured_snapshot, read_sql_snapshot
from .storage_sdk.relational import StructuredSchema


def export_authority(engine, *, expected_identity, namespace=None, source_frozen=False):
    schema = StructuredSchema(expected_identity.space.owner_id, LEGACY_METADATA,
        appendonly=('source_versions', 'parse_snapshots', 'citations'))
    columns = {table.name: tuple(table.c.keys()) for table in LEGACY_METADATA.tables.values()}
    snapshot = read_sql_snapshot(engine, namespace=namespace, expected_identity=expected_identity,
        columns={**columns, '_database_structured': ('version', 'schema_hash')}, source_frozen=source_frozen)
    if snapshot['tables'].pop('_database_structured') != [{'version': schema.version, 'schema_hash': schema.digest}]:
        raise ValueError('AUTHORITY_COMMIT_SOURCE_SCHEMA_MISMATCH')
    tables = _tables(snapshot['tables'])
    body = {'version': 1, 'source': {'space': vars(expected_identity.space),
        'dataset_id': expected_identity.dataset_id, 'schema_sha256': schema.digest,
        'migrations': snapshot['migrations']}, 'tables': tables}
    return {**body, 'sha256': content_hash(body)}


def validate_authority(package):
    if (type(package) is not dict or set(package) != {'version', 'source', 'tables', 'sha256'}
            or package['version'] != 1
            or content_hash({key: value for key, value in package.items() if key != 'sha256'}) != package['sha256']):
        raise ValueError('AUTHORITY_COMMIT_EXPORT_INTEGRITY')
    schema = StructuredSchema(package['source']['space']['owner_id'], LEGACY_METADATA,
        appendonly=('source_versions', 'parse_snapshots', 'citations'))
    if package['source']['schema_sha256'] != schema.digest or _tables(package['tables']) != package['tables']:
        raise ValueError('AUTHORITY_COMMIT_SOURCE_SCHEMA_MISMATCH')
    return package


def import_authority_sessions(target, authority, sessions, *, expected_identity, target_offline=False):
    validate_authority(authority)
    validate_sessions(sessions)
    if (expected_identity.dataset_id in {authority['source']['dataset_id'], sessions['source']['dataset_id']}
            or target._package.digest != knowledge_schema(expected_identity.space.owner_id).digest):
        raise ValueError('AUTHORITY_COMMIT_NEW_COMPOSITE_DATASET_REQUIRED')
    tables = {**_tables(authority['tables'], native=True), **deepcopy(sessions['tables'])}
    source_hash = content_hash({'authority': authority['sha256'], 'sessions': sessions['sha256']})
    receipt = {'version': 1, 'source_sha256': source_hash,
        'authority_source': authority['source'], 'sessions_source': sessions['source'],
        'target': {'space': vars(expected_identity.space), 'dataset_id': expected_identity.dataset_id},
        'counts': {name: len(rows) for name, rows in tables.items()},
        'target_records_sha256': content_hash(tables), 'publication_preserved': True,
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0}
    repeated = import_structured_snapshot(target, tables, expected_identity=expected_identity,
        source_sha256=source_hash, receipt=receipt, target_offline=target_offline)
    body = {**receipt, 'idempotent': repeated}
    return {**body, 'sha256': content_hash(body)}

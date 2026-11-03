from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import Integer

from .database_contracts import Migration, SpaceKey, StorageIdentity
from .knowledge_schema import LEGACY_METADATA as METADATA
from .storage_adapters.structured_transfer import (
    content_hash, import_structured_snapshot, read_sql_snapshot,
)
from .storage_sdk.relational import ExactString, TypedJSON, UtcDateTime


ARCHIVE = Path(__file__).resolve().parents[1] / 'config/storage/legacy-knowledge-migration-v1.json'
SOURCE_FORMATS = ('postgresql-managed-v1', 'sqlite-managed-v1', 'postgresql-bootstrap-v1')


def _record(table, row, *, native=False):
    if set(row) != set(table.c.keys()):
        raise ValueError('KNOWLEDGE_TRANSFER_COLUMNS')
    result = {}
    for column in table.c:
        value = row[column.name]
        if value is None:
            if not column.nullable:
                raise ValueError('KNOWLEDGE_TRANSFER_NULL')
        elif isinstance(column.type, TypedJSON):
            value = json.loads(value) if isinstance(value, str) else value
            json.dumps(value, allow_nan=False)
        elif isinstance(column.type, UtcDateTime):
            value = datetime.fromisoformat(value) if isinstance(value, str) else value
            if not isinstance(value, datetime):
                raise ValueError('KNOWLEDGE_TRANSFER_DATETIME')
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            value = value if native else value.isoformat()
        elif isinstance(column.type, Integer):
            if type(value) is not int:
                raise ValueError('KNOWLEDGE_TRANSFER_INTEGER')
        else:
            value = value.decode('utf-8') if isinstance(value, bytes) else value
            if not isinstance(value, str) or isinstance(column.type, ExactString) and len(value) > column.type.length:
                raise ValueError('KNOWLEDGE_TRANSFER_STRING')
        result[column.name] = value
    return result


def _tables(tables, *, native=False):
    if set(tables) != set(METADATA.tables):
        raise ValueError('KNOWLEDGE_TRANSFER_TABLES')
    result = {}
    for table in METADATA.sorted_tables:
        keys = tuple(column.name for column in table.primary_key)
        rows = [_record(table, row, native=native) for row in tables[table.name]]
        rows.sort(key=lambda row: tuple(row[key] for key in keys))
        if len({tuple(row[key] for key in keys) for row in rows}) != len(rows):
            raise ValueError('KNOWLEDGE_TRANSFER_DUPLICATE_IDENTITY')
        result[table.name] = rows
    return result


def _source_identity(source):
    return StorageIdentity(SpaceKey(**source['space']), source['dataset_id'])


def _lineage(source_format, owner, records):
    if source_format not in SOURCE_FORMATS:
        raise ValueError('KNOWLEDGE_TRANSFER_SOURCE_FORMAT')
    if source_format == 'postgresql-bootstrap-v1':
        if records:
            raise ValueError('KNOWLEDGE_TRANSFER_BOOTSTRAP_LINEAGE')
        return
    engine = source_format.split('-', 1)[0]
    archive = json.loads(ARCHIVE.read_text('utf-8'))['engines'][engine]['migrations']
    expected = []
    for record in archive:
        migration = Migration(record['version'], tuple(record['statements']))
        if migration.digest != record['digest']:
            raise ValueError('KNOWLEDGE_TRANSFER_ARCHIVE_INTEGRITY')
        expected.append({'schema_owner': owner, 'version': migration.version,
            'content_hash': migration.digest, 'dirty': 0, 'step': len(migration.statements)})
    if records != expected:
        raise ValueError('KNOWLEDGE_TRANSFER_SOURCE_LINEAGE')


def validate_export(package):
    if (not isinstance(package, dict) or set(package) != {'version', 'source', 'tables', 'counts', 'sha256'}
            or package['version'] != 1):
        raise ValueError('KNOWLEDGE_TRANSFER_VERSION')
    body = {key: value for key, value in package.items() if key != 'sha256'}
    if content_hash(body) != package['sha256']:
        raise ValueError('KNOWLEDGE_TRANSFER_INTEGRITY')
    source = package['source']
    if set(source) != {'format', 'space', 'dataset_id', 'namespace', 'migrations'}:
        raise ValueError('KNOWLEDGE_TRANSFER_SOURCE')
    identity = _source_identity(source)
    _lineage(source['format'], identity.space.owner_id, source['migrations'])
    tables = _tables(package['tables'])
    if tables != package['tables'] or package['counts'] != {name: len(rows) for name, rows in tables.items()}:
        raise ValueError('KNOWLEDGE_TRANSFER_RECORDS')
    builds = {(row['tenant_id'], row['kb_id'], row['build_id']): row for row in tables['builds']}
    for row in tables['knowledge_bases']:
        if row['manifest_id'] is not None:
            build = builds.get((row['tenant_id'], row['kb_id'], row['manifest_id']))
            if build is None or build['status'] != 'PUBLISHED':
                raise ValueError('KNOWLEDGE_TRANSFER_PUBLISHED_POINTER')
    return package


def export_legacy_knowledge(engine, *, expected_identity, source_format, namespace=None, source_frozen=False):
    if source_format not in SOURCE_FORMATS:
        raise ValueError('KNOWLEDGE_TRANSFER_SOURCE_FORMAT')
    snapshot = read_sql_snapshot(engine, namespace=namespace, expected_identity=expected_identity,
        columns={table.name: tuple(table.c.keys()) for table in METADATA.tables.values()},
        source_frozen=source_frozen)
    source = {'format': source_format, 'space': vars(expected_identity.space),
        'dataset_id': expected_identity.dataset_id, 'namespace': namespace, 'migrations': snapshot['migrations']}
    tables = _tables(snapshot['tables'])
    body = {'version': 1, 'source': source, 'tables': tables,
            'counts': {name: len(rows) for name, rows in tables.items()}}
    return validate_export({**body, 'sha256': content_hash(body)})


def import_knowledge(target, package, *, expected_identity, target_offline=False):
    validate_export(package)
    if target_offline is not True:
        raise ValueError('TRANSFER_TARGET_MUST_BE_OFFLINE')
    if expected_identity.dataset_id == package['source']['dataset_id']:
        raise ValueError('KNOWLEDGE_TRANSFER_REQUIRES_NEW_DATASET')
    transformed = deepcopy(package['tables'])
    pointers = [{key: row[key] for key in ('tenant_id', 'kb_id', 'manifest_id')}
                for row in transformed['knowledge_bases'] if row['manifest_id'] is not None]
    for row in transformed['tenant_state']:
        row['revision'] += 1
    for row in transformed['knowledge_bases']:
        row['manifest_id'], row['revision'] = None, row['revision'] + 1
    resets = []
    for row in transformed['builds']:
        if row['status'] in {'PUBLISHED', 'VALIDATED'}:
            resets.append({key: row[key] for key in ('tenant_id', 'kb_id', 'build_id', 'status')})
            row['status'] = 'STAGING'
    receipt = {'version': 1, 'source_sha256': package['sha256'], 'source': package['source'],
        'target': {'space': vars(expected_identity.space), 'dataset_id': expected_identity.dataset_id},
        'counts': dict(package['counts']), 'target_records_sha256': content_hash(transformed),
        'publication_reset': {'original_pointers': pointers, 'build_statuses': resets,
            'tenant_revision_increment': 1, 'kb_revision_increment': 1},
        'republication_required': True, 'search_indexes_copied': False, 'original_objects_copied': False,
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0}
    records = {name: [] for name in target._package.metadata.tables}
    records.update(_tables(transformed, native=True))
    repeated = import_structured_snapshot(target, records,
        expected_identity=expected_identity, source_sha256=package['sha256'], receipt=receipt,
        target_offline=target_offline)
    body = {**receipt, 'idempotent': repeated}
    return {**body, 'sha256': content_hash(body)}


def validate_receipt(receipt):
    body = {key: value for key, value in receipt.items() if key != 'sha256'}
    if content_hash(body) != receipt.get('sha256'):
        raise ValueError('KNOWLEDGE_TRANSFER_RECEIPT_INTEGRITY')
    return receipt

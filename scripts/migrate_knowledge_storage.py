import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.database_contracts import SpaceKey, StorageIdentity
from kb_platform.knowledge_schema import knowledge_schema
from kb_platform.knowledge_transfer import SOURCE_FORMATS, export_legacy_knowledge, import_knowledge, validate_export
from kb_platform.storage_catalog import default_storage_providers
from kb_platform.storage_sdk import ExistingDataset


def _write(path, value):
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        output.write('\n')


def _source_engine(value):
    url = make_url(value)
    drivers = {'postgresql': 'postgresql+psycopg', 'postgres': 'postgresql+psycopg',
               'mysql': 'mysql+pymysql', 'sqlite': 'sqlite'}
    if url.get_backend_name() not in drivers:
        raise ValueError('KNOWLEDGE_TRANSFER_SQL_SOURCE_REQUIRED')
    return create_engine(url.set(drivername=drivers[url.get_backend_name()]))


def _import(args):
    package = validate_export(json.loads(args.input.read_text('utf-8')))
    configuration = json.loads(args.target_config.read_text('utf-8'))
    if configuration.get('version') != 2:
        raise ValueError('INVALID_STORAGE_CONFIGURATION')
    binding = configuration['bindings'][args.consumer][args.slot]
    if binding.get('dataset_id') != args.expected_target_dataset:
        raise ValueError('KNOWLEDGE_TRANSFER_EXPECTED_TARGET_MISMATCH')
    definition = configuration['services'][binding['service']]
    row = configuration['instances'][definition['instance']]
    provider = default_storage_providers()[row['provider']].factory(row['config'])
    try:
        space = SpaceKey(binding.get('application_id', configuration['application_id']),
                         binding['owner'], binding['space'])
        exported = next(item for item in provider.exports() if item.name == definition['export'])
        target = exported.open_space(space, ExistingDataset(args.expected_target_dataset))
        target.prepare(knowledge_schema(space.owner_id))
        target.seal()
        return import_knowledge(target, package,
            expected_identity=StorageIdentity(space, args.expected_target_dataset), target_offline=args.target_offline)
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline knowledge record transfer; explicit source identity and new initialized target required. No models or search publication.')
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    export.add_argument('--source-url-env', required=True)
    export.add_argument('--source-format', required=True, choices=SOURCE_FORMATS)
    export.add_argument('--source-namespace')
    export.add_argument('--source-application', required=True)
    export.add_argument('--source-owner', required=True)
    export.add_argument('--source-space', required=True)
    export.add_argument('--expected-source-dataset', required=True)
    export.add_argument('--source-frozen', action='store_true', required=True)
    clone = commands.add_parser('import')
    clone.add_argument('--input', type=Path, required=True)
    clone.add_argument('--target-config', type=Path, required=True)
    clone.add_argument('--consumer', default='repository')
    clone.add_argument('--slot', default='primary')
    clone.add_argument('--expected-target-dataset', required=True)
    clone.add_argument('--target-offline', action='store_true', required=True)
    for child in (export, clone):
        child.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    if args.command == 'export':
        engine = _source_engine(os.environ[args.source_url_env])
        try:
            identity = StorageIdentity(SpaceKey(args.source_application, args.source_owner, args.source_space),
                                       args.expected_source_dataset)
            result = export_legacy_knowledge(engine, expected_identity=identity,
                source_format=args.source_format, namespace=args.source_namespace, source_frozen=args.source_frozen)
        finally:
            engine.dispose()
        _write(args.output / 'knowledge-export.json', result)
        print(json.dumps({'sha256': result['sha256'], 'counts': result['counts'], 'new_remote_calls': 0}))
    else:
        result = _import(args)
        _write(args.output / 'receipt.json', result)
        print(json.dumps({key: result[key] for key in ('sha256', 'counts', 'idempotent', 'republication_required')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

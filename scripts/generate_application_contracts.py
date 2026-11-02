import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.application_knowledge import application_schemas
from kb_platform.application_storage import storage_methods
from kb_platform.knowledge_authority_contract import authority_schema_descriptor
from kb_platform.knowledge_capabilities import CATALOG, registry
from kb_platform.knowledge_model_ports import METHODS as MODEL_METHODS
from kb_platform.knowledge_platform_contract import METHODS as PLATFORM_METHODS
from kb_platform.knowledge_semantic_contract import METHODS as SEMANTIC_METHODS


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'config/contracts/knowledge-v1.json'


def contract_document():
    selected = registry({('storage.service', 1): storage_methods(),
        ('knowledge.model.capabilities', 1): MODEL_METHODS,
        ('knowledge.platform', 1): PLATFORM_METHODS,
        ('knowledge.semantic', 1): SEMANTIC_METHODS})
    schemas = application_schemas()
    for definition in CATALOG:
        schemas[(definition['implementation'], definition['version'])]
        for contract in definition['provides'].values():
            selected.methods(contract)
        for requirement in definition['requires'].values():
            selected.methods(requirement['contract'])
    return {'format': 'evidence-plugin-contracts/1', 'catalog': deepcopy(CATALOG),
        'contracts': [{'id': identity, 'major': major,
            **({'dataset_schema': authority_schema_descriptor()} if identity == 'knowledge.authority.storage' else {}),
            'methods': {
            name: {'request': method.request, 'response': method.response}
            for name, method in sorted(methods.items())}}
            for (identity, major), methods in sorted(selected.contracts.items())],
        'schemas': [{'implementation': implementation, 'version': version, 'schema': schema}
            for (implementation, version), schema in sorted(schemas.items())],
        'error': {'type': 'object', 'properties': {
            'code': {'type': 'string', 'minLength': 1}, 'message': {'type': 'string'},
            'status_code': {'type': 'integer', 'minimum': 400, 'maximum': 599},
            'details': {'type': 'object'}},
            'required': ['code', 'message', 'status_code', 'details'], 'additionalProperties': False}}


def main():
    parser = argparse.ArgumentParser(description='Export the public knowledge application plugin contracts.')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    value = json.dumps(contract_document(), ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if args.check:
        if not args.output.is_file() or args.output.read_text('utf-8') != value:
            parser.error('public contract artifact is stale; regenerate with this command without --check')
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(value, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

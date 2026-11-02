import json
from pathlib import Path

from .plugin_contracts import schema_validator, wire
from .plugin_native import PluginError


def load_plugin_catalog(path):
    source = Path(path).resolve(strict=True)
    document = wire(json.loads(source.read_text('utf-8')))
    if (not isinstance(document, dict) or set(document) != {'version', 'definitions', 'schemas', 'root'}
            or type(document['version']) is not int or document['version'] != 1
            or not isinstance(document['definitions'], list) or len(document['definitions']) > 256
            or not isinstance(document['schemas'], list)
            or not isinstance(document['root'], str) or not document['root']):
        raise PluginError('INVALID_PLUGIN_CATALOG')
    root = (source.parent / document['root']).resolve(strict=True)
    if not root.is_dir():
        raise PluginError('INVALID_PLUGIN_CATALOG_ROOT')
    definitions = {}
    for definition in document['definitions']:
        if (not isinstance(definition, dict)
                or not {'implementation', 'version', 'command', 'provides', 'requires'} <= set(definition)
                or set(definition) - {'implementation', 'version', 'executor', 'command', 'provides', 'requires'}
                or definition.get('executor', 'process') != 'process'
                or any(not isinstance(definition[key], str) or not definition[key]
                       for key in ('implementation', 'version'))):
            raise PluginError('INVALID_PLUGIN_CATALOG_DEFINITION')
        key = definition['implementation'], definition['version']
        if key in definitions:
            raise PluginError('DUPLICATE_PLUGIN_CATALOG_DEFINITION')
        definitions[key] = definition
    schemas = {}
    for entry in document['schemas']:
        if (not isinstance(entry, dict) or set(entry) != {'implementation', 'version', 'schema'}
                or any(not isinstance(entry[key], str) for key in ('implementation', 'version'))):
            raise PluginError('INVALID_PLUGIN_CATALOG_SCHEMA')
        key = entry['implementation'], entry['version']
        if key in schemas or key not in definitions:
            raise PluginError('INVALID_PLUGIN_CATALOG_SCHEMA')
        schema_validator(entry['schema'])
        schemas[key] = entry['schema']
    if set(schemas) != set(definitions):
        raise PluginError('PLUGIN_CATALOG_SCHEMA_REQUIRED')
    return {'extra_catalog': list(definitions.values()), 'schemas': schemas, 'process_root': root}

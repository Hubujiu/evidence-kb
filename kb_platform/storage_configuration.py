import argparse
from copy import deepcopy
import json
from pathlib import Path


def convert_database_configuration(configuration):
    if (not isinstance(configuration, dict) or set(configuration) != {
            'version', 'application_id', 'routes', 'bindings'} or configuration['version'] != 1):
        raise ValueError('INVALID_DATABASE_CONFIGURATION')
    result = {'version': 2, 'application_id': configuration['application_id'],
              'instances': {}, 'services': {}, 'bindings': {}}
    for name, route in configuration['routes'].items():
        options = deepcopy(route)
        provider = options.pop('provider')
        result['instances'][name] = {'provider': 'postgresql' if provider == 'postgres' else provider,
                                     'config': options}
        result['services'][name + '.relational'] = {'instance': name, 'export': 'relational'}
    for consumer, slots in configuration['bindings'].items():
        result['bindings'][consumer] = {}
        for slot, definition in slots.items():
            binding = deepcopy(definition)
            route = binding.pop('route')
            binding['service'] = route + '.relational'
            if 'expected_dataset_id' in binding:
                binding['dataset_id'] = binding.pop('expected_dataset_id')
            result['bindings'][consumer][slot] = binding
    return result


def main():
    parser = argparse.ArgumentParser(description='Explicitly convert or initialize storage configuration.')
    parser.add_argument('operation', choices=('convert-databases', 'initialize'))
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.input.resolve() == args.output.resolve():
        parser.error('Output must be a new file; input and prior evidence remain unchanged')
    configuration = json.loads(args.input.read_text('utf-8'))
    if args.operation == 'convert-databases':
        result = convert_database_configuration(configuration)
    else:
        from .storage_catalog import initialize_configuration

        result = initialize_configuration(configuration)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'status': 'COMPLETE', 'operation': args.operation, 'output': str(args.output)}))


if __name__ == '__main__':
    main()

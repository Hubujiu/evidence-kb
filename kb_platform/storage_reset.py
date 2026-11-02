import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from .storage_catalog import default_storage_providers, initialize_configuration
from .storage_sdk import EphemeralCacheSpace, ExistingDataset, SpaceKey, StorageProvider


BUSINESS_ROLES = frozenset({'knowledge', 'sessions', 'context', 'cache'})
PRESERVED_ROLES = frozenset({'ledger', 'identity', 'external'})
DEFAULT_ROLES = {
    'repository': 'knowledge', 'search': 'knowledge', 'originals': 'knowledge',
    'knowledge': 'knowledge', 'retrieval': 'knowledge',
    'store': 'sessions', 'sessions': 'sessions', 'context': 'context', 'cache': 'cache',
    'provider': 'ledger', 'model-budget': 'ledger', 'models.budget': 'ledger',
    'identity': 'identity', 'access': 'identity',
}


@dataclass(frozen=True)
class ResetPlan:
    configuration: dict
    consumers: tuple[str, ...]


def _validate(configuration, roles, providers):
    if (not isinstance(configuration, dict) or set(configuration) != {
            'version', 'application_id', 'instances', 'services', 'bindings'}
            or type(configuration['version']) is not int or configuration['version'] != 2
            or not isinstance(configuration['application_id'], str)
            or not configuration['application_id'].strip()
            or any(not isinstance(configuration[key], dict) for key in ('instances', 'services', 'bindings'))
            or not isinstance(providers, dict)
            or any(not isinstance(provider, StorageProvider) for provider in providers.values())):
        raise ValueError('RESET_INVALID_STORAGE_CONFIGURATION')
    if (not isinstance(roles, dict) or set(roles) != set(configuration['bindings'])
            or any(not isinstance(role, str) or role not in BUSINESS_ROLES | PRESERVED_ROLES
                   for role in roles.values())):
        raise ValueError('RESET_EXPLICIT_CONSUMER_ROLES_REQUIRED')
    if any(DEFAULT_ROLES.get(name) == 'ledger' and role != 'ledger' for name, role in roles.items()):
        raise ValueError('RESET_LEDGER_ROLE_IMMUTABLE')
    if any(DEFAULT_ROLES.get(name) in BUSINESS_ROLES and role not in BUSINESS_ROLES
           for name, role in roles.items()):
        raise ValueError('RESET_RELATED_BUSINESS_STATE_REQUIRED')
    json.dumps(configuration, allow_nan=False)
    instances, services = configuration['instances'], configuration['services']
    for name, instance in instances.items():
        if (not isinstance(name, str) or not name or not isinstance(instance, dict)
                or set(instance) != {'provider', 'config'}
                or not isinstance(instance['provider'], str) or instance['provider'] not in providers
                or not isinstance(instance['config'], dict)):
            raise ValueError('RESET_INVALID_STORAGE_INSTANCE')
    for name, service in services.items():
        if (not isinstance(name, str) or not name or not isinstance(service, dict)
                or set(service) != {'instance', 'export'}
                or not isinstance(service['instance'], str) or service['instance'] not in instances
                or not isinstance(service['export'], str)
                or service['export'] not in providers[instances[service['instance']]['provider']].exports):
            raise ValueError('RESET_INVALID_STORAGE_SERVICE')
    ownership = {}
    for consumer, bindings in configuration['bindings'].items():
        if not isinstance(consumer, str) or not consumer or not isinstance(bindings, dict) or not bindings:
            raise ValueError('RESET_INVALID_STORAGE_CONSUMER')
        for name, binding in bindings.items():
            if (not isinstance(name, str) or not name or not isinstance(binding, dict)
                    or not {'service', 'owner', 'space'} <= set(binding)
                    or set(binding) - {'service', 'owner', 'space', 'application_id', 'dataset_id', 'cache_id'}
                    or not isinstance(binding['service'], str) or binding['service'] not in services
                    or ('dataset_id' in binding) == ('cache_id' in binding)):
                raise ValueError('RESET_INITIALIZED_BINDING_REQUIRED')
            space = SpaceKey(binding.get('application_id', configuration['application_id']),
                             binding['owner'], binding['space'])
            target = (ExistingDataset(binding['dataset_id']) if 'dataset_id' in binding
                      else EphemeralCacheSpace(binding['cache_id']))
            identity = services[binding['service']]['instance'], target
            if ownership.setdefault(space, identity) != identity:
                raise ValueError('RESET_STORAGE_SPACE_CONFLICT')


def plan_business_reset(configuration, *, roles=None, provider_catalog=None):
    providers = default_storage_providers() if provider_catalog is None else provider_catalog
    if roles is None:
        bindings = configuration.get('bindings', {}) if isinstance(configuration, dict) else {}
        roles = {name: DEFAULT_ROLES.get(name) for name in bindings} if isinstance(bindings, dict) else {}
    _validate(configuration, roles, providers)
    consumers = tuple(sorted(name for name, role in roles.items() if role in BUSINESS_ROLES))
    if not consumers:
        raise ValueError('RESET_NO_BUSINESS_CONSUMERS')
    selected = deepcopy(configuration)
    spaces, caches = {}, {}
    generation = uuid4().hex
    for consumer in consumers:
        for binding in selected['bindings'][consumer].values():
            original = SpaceKey(binding.get('application_id', selected['application_id']),
                                binding['owner'], binding['space'])
            binding['space'] = spaces.setdefault(original, f'reset-{generation}-{len(spaces)}')
            if 'dataset_id' in binding:
                del binding['dataset_id']
            else:
                binding['cache_id'] = caches.setdefault((original, binding['cache_id']), uuid4().hex)
    return ResetPlan(selected, consumers)


def initialize_business_reset(plan, *, provider_catalog=None):
    selected = deepcopy(plan.configuration)
    business = {name: selected['bindings'][name] for name in plan.consumers}
    services = {binding['service'] for bindings in business.values() for binding in bindings.values()}
    instances = {selected['services'][name]['instance'] for name in services}
    initialization = {**selected, 'bindings': business,
        'services': {name: selected['services'][name] for name in services},
        'instances': {name: selected['instances'][name] for name in instances}}
    initialized = initialize_configuration(initialization, provider_catalog=provider_catalog)
    selected['bindings'].update(initialized['bindings'])
    return selected


def reset_to_file(source, output, *, roles=None, provider_catalog=None):
    source, output = Path(source), Path(output)
    if output.exists() or output.is_symlink() or source.resolve() == output.resolve():
        raise ValueError('RESET_OUTPUT_MUST_BE_NEW')
    configuration = json.loads(source.read_text('utf-8'))
    plan = plan_business_reset(configuration, roles=roles, provider_catalog=provider_catalog)
    descriptor, temporary = tempfile.mkstemp(prefix='.storage-reset-', suffix='.json', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            initialized = initialize_business_reset(plan, provider_catalog=provider_catalog)
            json.dump(initialized, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'status': 'COMPLETE', 'operation': 'reset-business', 'output': str(output),
            'reset_consumers': list(plan.consumers),
            'preserved_consumers': sorted(set(configuration['bindings']) - set(plan.consumers))}


def main():
    parser = argparse.ArgumentParser(description='Create empty business datasets and publish a new storage configuration.')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--roles', type=Path)
    args = parser.parse_args()
    try:
        roles = json.loads(args.roles.read_text('utf-8')) if args.roles is not None else None
        result = reset_to_file(args.input, args.output, roles=roles)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

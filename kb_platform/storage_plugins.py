from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from types import MappingProxyType

from .plugin_configuration import config_schema, environment_inputs
from .plugin_host import PluginSpec
from .storage_sdk import (
    EphemeralCacheSpace, ExistingDataset, SpaceKey, StorageError, StorageProvider, StorageSlot,
)
from .storage_service import StorageService


def require_storage(ctx, slot):
    declared = {item.name: item for item in ctx.spec.storage_slots}
    name = slot.name if isinstance(slot, StorageSlot) else slot
    if (not isinstance(name, str) or name not in declared
            or isinstance(slot, StorageSlot) and declared[name] != slot):
        raise StorageError('UNDECLARED_STORAGE_SLOT')
    mapping = getattr(ctx, '_storage_slots', {})
    if name not in mapping:
        raise StorageError('UNBOUND_STORAGE_SLOT', slot=name)
    bound = ctx.require(mapping[name])
    return bound.for_consumer(ctx.identity, ctx.lease, consumer_name=ctx.plugin_id)


def storage_plugins(service=None):
    def mount(ctx, config):
        registry = StorageService() if service is None else service
        if service is None:
            ctx.defer(registry.close)
        ctx.provide('storage.registry', registry)

    return (PluginSpec('storage.service', ('storage.registry',), (), mount,
                       config_schema=config_schema()),)


def compile_storage_plugins(profile, catalog, configuration, *, provider_catalog, service=None):
    if (not isinstance(configuration, dict) or set(configuration) != {
            'version', 'application_id', 'instances', 'services', 'bindings'}
            or type(configuration.get('version')) is not int or configuration['version'] != 2
            or not isinstance(configuration.get('application_id'), str)
            or not configuration['application_id']
            or any(not isinstance(configuration[key], dict) for key in ('instances', 'services', 'bindings'))
            or not isinstance(provider_catalog, dict)
            or any(not isinstance(item, StorageProvider) for item in provider_catalog.values())):
        raise ValueError('INVALID_STORAGE_CONFIGURATION')
    selected, specs = deepcopy(profile), list(catalog)
    by_spec = {spec.key: spec for spec in specs}
    by_id = {row['id']: row for row in selected['plugins'] if not row.get('disabled')}
    if 'storage.service' in by_spec or any(row['id'] == 'storage.service' for row in selected['plugins']):
        raise ValueError('STORAGE_REGISTRY_ALREADY_CONFIGURED')
    if len(by_spec) != len(specs) or any(row['use'] not in by_spec for row in by_id.values()):
        raise ValueError('UNKNOWN_STORAGE_CONSUMER_IMPLEMENTATION')
    instances = {}
    for name, definition in configuration['instances'].items():
        if (not isinstance(name, str) or not name or not isinstance(definition, dict)
                or set(definition) != {'provider', 'config'}
                or not isinstance(definition['provider'], str)
                or definition['provider'] not in provider_catalog
                or not isinstance(definition['config'], dict)):
            raise ValueError('INVALID_STORAGE_INSTANCE')
        instances[name] = (provider_catalog[definition['provider']], deepcopy(definition['config']))
    services = {}
    for name, definition in configuration['services'].items():
        if (not isinstance(name, str) or not name or not isinstance(definition, dict)
                or set(definition) != {'instance', 'export'}
                or not isinstance(definition['instance'], str) or definition['instance'] not in instances
                or not isinstance(definition['export'], str)
                or definition['export'] not in instances[definition['instance']][0].exports):
            raise ValueError('INVALID_STORAGE_SERVICE')
        services[name] = (definition['instance'], definition['export'])
    bindings = configuration['bindings']
    if set(bindings) - set(by_id):
        raise ValueError('UNKNOWN_STORAGE_CONSUMER')
    planned, ownership = [], {}
    for consumer, row in by_id.items():
        declared = {slot.name: slot for slot in by_spec[row['use']].storage_slots}
        configured = bindings.get(consumer, {})
        if not isinstance(configured, dict) or set(configured) != set(declared):
            raise ValueError('STORAGE_SLOT_CONFIGURATION_MISMATCH')
        for name, binding in configured.items():
            if (not isinstance(binding, dict) or set(binding) - {
                    'service', 'owner', 'space', 'application_id', 'dataset_id', 'cache_id'}
                    or not isinstance(binding.get('service'), str) or binding['service'] not in services
                    or ('dataset_id' in binding) == ('cache_id' in binding)):
                raise ValueError('INVALID_STORAGE_BINDING')
            space = SpaceKey(binding.get('application_id', configuration['application_id']),
                             binding.get('owner'), binding.get('space'))
            target = (ExistingDataset(binding['dataset_id']) if 'dataset_id' in binding
                      else EphemeralCacheSpace(binding['cache_id']))
            service_name = binding['service']
            instance, export = services[service_name]
            contracts = instances[instance][0].exports[export]
            slot = declared[name]
            if not any(contract.satisfies(slot.contract) for contract in contracts):
                raise StorageError('STORAGE_CONTRACT_MISSING', consumer=consumer, slot=name,
                                   service=service_name, required=slot.contract.key,
                                   actual=tuple(contract.key for contract in contracts))
            previous = ownership.setdefault(space, (instance, target))
            if previous != (instance, target):
                raise ValueError('STORAGE_SPACE_SERVICE_CONFLICT')
            planned.append((consumer, slot, service_name, space, target))
    additions = list(storage_plugins(service))
    entries = [{'id': 'storage.service', 'use': 'storage.service'}]
    for name, (provider, config) in instances.items():
        marker = _key('storage.instance', name)
        exports = {service_name: export for service_name, (instance, export) in services.items() if instance == name}
        additions.append(_instance_plugin(marker, provider, config, exports,
                                         configuration['instances'][name]['provider']))
        entries.append({'id': marker, 'use': marker})
    mappings = {}
    for consumer, slot, name, space, target in planned:
        marker = _key('storage.instance', services[name][0])
        bound = _key('storage.bound', consumer, slot.name)
        additions.append(_binding_plugin(bound, marker, name, slot, space, target, consumer))
        entries.append({'id': bound, 'use': bound})
        mappings.setdefault(consumer, {})[slot.name] = bound
    for consumer, mapping in mappings.items():
        row = by_id[consumer]
        key = _key('storage.consumer', consumer)
        additions.append(_consumer_plugin(by_spec[row['use']], key, mapping))
        row['use'] = key
    keys = [spec.key for spec in (*specs, *additions)]
    identities = [row['id'] for row in (*selected['plugins'], *entries)]
    if len(keys) != len(set(keys)) or len(identities) != len(set(identities)):
        raise ValueError('STORAGE_GENERATED_KEY_COLLISION')
    selected['plugins'] = [*entries, *selected['plugins']]
    return selected, tuple((*specs, *additions))


def _instance_plugin(marker, provider, definition, exports, provider_name):
    def mount(ctx, config):
        try:
            instance = provider.factory(deepcopy(definition))
        except StorageError:
            raise
        except Exception:
            raise StorageError('STORAGE_PROVIDER_UNAVAILABLE') from None
        if not callable(getattr(instance, 'close', None)):
            raise StorageError('STORAGE_PROVIDER_INVALID')
        ctx.defer(instance.close)
        actual = tuple(instance.exports())
        lookup = {export.name: export for export in actual}
        if (len(actual) != len(lookup) or set(lookup) != set(provider.exports)
                or any(lookup[name].contracts != tuple(contracts) for name, contracts in provider.exports.items())):
            raise StorageError('STORAGE_PROVIDER_EXPORT_MISMATCH')
        registry = ctx.require('storage.registry')
        registrations = {}
        configuration_sha256 = _fingerprint([provider_name, definition,
            environment_inputs(_environment_names(definition))])
        for name, export_name in exports.items():
            dispose = registry.register(name, lookup[export_name], owner_identity=ctx.identity,
                                        owner_lease=ctx.lease,
                                        registration_id=ctx.registration_id('storage_export'),
                                        configuration_sha256=configuration_sha256)
            ctx.defer(dispose)
            registrations[name] = MappingProxyType(dict(registry.inspect()[name]))
        ctx.provide(marker, MappingProxyType(registrations))

    def inputs(config):
        return {**environment_inputs(_environment_names(definition)),
                'storage.instance': _fingerprint([provider_name, definition, exports])}

    return PluginSpec(marker, (marker,), ('storage.registry',), mount,
                      config_schema=config_schema(), config_inputs=inputs)


def _environment_names(value):
    names = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith('_env') and isinstance(item, str):
                names.add(item)
            else:
                names.update(_environment_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_environment_names(item))
    return names


def _binding_plugin(key, marker, name, slot, space, target, consumer):
    def mount(ctx, config):
        registration = ctx.require(marker)[name]
        registry = ctx.require('storage.registry')
        inspection = registry.inspect().get(name)
        if (inspection is None or inspection['registration_id'] != registration['registration_id']
                or inspection['owner_identity'] != registration['owner_identity']):
            raise StorageError('STORAGE_PROVIDER_OWNER_MISMATCH')
        grant = registry.grant(name, slot.contract, space, consumer, target=target)
        ctx.provide(key, registry.bind(grant, owner_lease=ctx.lease))

    def inputs(config):
        identity = target.dataset_id if isinstance(target, ExistingDataset) else target.cache_id
        return {'storage.binding': _fingerprint([name, slot.contract.key, space.key,
                                                type(target).__name__, identity, consumer])}

    return PluginSpec(key, (key,), ('storage.registry', marker), mount,
                      config_schema=config_schema(), config_inputs=inputs)


def _consumer_plugin(spec, key, mapping):
    frozen = MappingProxyType(dict(mapping))

    def mount(ctx, config):
        ctx._storage_slots = frozen
        return spec.mount(ctx, config)

    return replace(spec, key=key, requires=tuple(dict.fromkeys((*spec.requires, *frozen.values()))), mount=mount)


def _key(prefix, *parts):
    return prefix + '.' + '.'.join(part.encode('utf-8').hex() for part in parts)


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode()).hexdigest()

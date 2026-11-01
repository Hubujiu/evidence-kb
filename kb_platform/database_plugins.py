from copy import deepcopy
from dataclasses import replace
import os
from types import MappingProxyType

from .database_contracts import DatabaseError, SpaceKey
from .database_providers import MySQLProvider, PostgresProvider, SQLiteProvider
from .database_service import DatabaseService
from .plugin_configuration import config_schema, environment_inputs
from .plugin_host import PluginSpec


def require_database(ctx, slot):
    mapping = getattr(ctx, '_database_slots', None)
    if mapping is None or slot not in mapping:
        raise DatabaseError('UNDECLARED_DATABASE_SLOT')
    bound = ctx.require(mapping[slot])
    return bound.for_consumer(ctx.identity, ctx.lease, consumer_name=ctx.plugin_id)


def database_plugins(service=None):
    def mount(ctx, config):
        registry = service if service is not None else DatabaseService()
        if service is None:
            ctx.defer(registry.close)
        ctx.provide('databases.registry', registry)

    return (PluginSpec('databases.service', ('databases.registry',), (), mount,
                       config_schema=config_schema()),)


def compile_database_plugins(profile, catalog, configuration, *, slots=None, service=None):
    if (not isinstance(configuration, dict) or set(configuration) - {
            'version', 'application_id', 'routes', 'bindings'} or configuration.get('version') != 1):
        raise ValueError('INVALID_DATABASE_CONFIGURATION')
    application = configuration.get('application_id')
    routes, bindings = configuration.get('routes'), configuration.get('bindings')
    if not isinstance(application, str) or not application or not isinstance(routes, dict) or not isinstance(bindings, dict):
        raise ValueError('INVALID_DATABASE_CONFIGURATION')
    selected, specs = deepcopy(profile), list(catalog)
    by_spec = {spec.key: spec for spec in specs}
    by_id = {row['id']: row for row in selected['plugins'] if not row.get('disabled')}
    declared = {spec.key: spec.storage_slots for spec in specs if spec.storage_slots}
    declared.update({} if slots is None else slots)
    if set(bindings) - set(by_id):
        raise ValueError('UNKNOWN_DATABASE_CONSUMER')
    if any(row['use'] not in by_spec for row in by_id.values()):
        raise ValueError('UNKNOWN_DATABASE_CONSUMER_IMPLEMENTATION')
    if 'databases.service' in by_spec or any(row['id'] == 'databases.service' for row in selected['plugins']):
        raise ValueError('DATABASE_REGISTRY_ALREADY_CONFIGURED')
    definitions = {}
    for name, route in routes.items():
        if not isinstance(name, str) or not name:
            raise ValueError('INVALID_DATABASE_ROUTE')
        definitions[name] = _validate_route(route)
    planned = []
    ownership = {}
    for consumer, slot_map in bindings.items():
        spec = by_spec[by_id[consumer]['use']]
        expected = tuple(declared.get(spec.key, ()))
        if not isinstance(slot_map, dict) or set(slot_map) != set(expected) or not expected:
            raise ValueError('DATABASE_SLOT_CONFIGURATION_MISMATCH')
        for slot, value in slot_map.items():
            if (not isinstance(slot, str) or not slot or not isinstance(value, dict) or
                    set(value) - {'route', 'owner', 'space', 'expected_dataset_id', 'application_id'} or
                    value.get('route') not in definitions):
                raise ValueError('INVALID_DATABASE_BINDING')
            key = SpaceKey(value.get('application_id', application), value.get('owner'), value.get('space'))
            expected_id = value.get('expected_dataset_id')
            if expected_id is not None and (not isinstance(expected_id, str) or not expected_id):
                raise ValueError('INVALID_DATABASE_DATASET_ID')
            previous = ownership.setdefault(key, (value['route'], expected_id))
            if previous != (value['route'], expected_id):
                raise ValueError('DATABASE_SPACE_ROUTE_CONFLICT')
            planned.append((consumer, slot, value['route'], key, expected_id))
    for consumer, row in by_id.items():
        expected = declared.get(row['use'], ())
        if expected and consumer not in bindings:
            raise ValueError('DATABASE_REQUIRED_SLOT_MISSING')
    additions = list(database_plugins(service))
    entries = [{'id': 'databases.service', 'use': 'databases.service'}]
    for name, definition in definitions.items():
        marker = _key('databases.route', name)
        additions.append(_route_plugin(marker, name, definition))
        entries.append({'id': marker, 'use': marker})
    slot_services = {}
    for consumer, slot, name, key, expected_id in planned:
        marker = _key('databases.route', name)
        bound_key = _key('databases.bound', consumer, slot)
        additions.append(_binding_plugin(bound_key, marker, name, key, expected_id, consumer))
        entries.append({'id': bound_key, 'use': bound_key})
        slot_services.setdefault(consumer, {})[slot] = bound_key
    for consumer, mapping in slot_services.items():
        row = by_id[consumer]
        spec = by_spec[row['use']]
        use = _key('databases.consumer', consumer)
        wrapped = _consumer_plugin(spec, use, mapping)
        additions.append(wrapped)
        row['use'] = use
    keys = [spec.key for spec in (*specs, *additions)]
    identities = [row['id'] for row in (*selected['plugins'], *entries)]
    if len(keys) != len(set(keys)) or len(identities) != len(set(identities)):
        raise ValueError('DATABASE_GENERATED_KEY_COLLISION')
    selected['plugins'] = [*entries, *selected['plugins']]
    return selected, tuple((*specs, *additions))


def _validate_route(route):
    if not isinstance(route, dict) or set(route) - {
            'provider', 'root', 'root_env', 'url', 'url_env', 'migration_url', 'migration_url_env',
            'provision', 'schema', 'database'}:
        raise ValueError('INVALID_DATABASE_ROUTE')
    provider = route.get('provider')
    if provider not in {'sqlite', 'postgresql', 'postgres', 'mysql'}:
        raise ValueError('UNKNOWN_DATABASE_PROVIDER')
    location = 'root' if provider == 'sqlite' else 'url'
    if sum(key in route for key in (location, location + '_env')) != 1:
        raise ValueError('DATABASE_LOCATION_REQUIRED')
    forbidden = {'url', 'url_env', 'migration_url', 'migration_url_env', 'schema', 'database', 'provision'} if provider == 'sqlite' else {'root', 'root_env'}
    if set(route) & forbidden:
        raise ValueError('INVALID_DATABASE_PROVIDER_OPTION')
    if 'migration_url' in route and 'migration_url_env' in route:
        raise ValueError('DUPLICATE_DATABASE_MIGRATION_URL')
    for key, value in route.items():
        if key == 'provision':
            if type(value) is not bool:
                raise ValueError('INVALID_DATABASE_PROVIDER_OPTION')
        elif not isinstance(value, str) or not value:
            raise ValueError('INVALID_DATABASE_PROVIDER_OPTION')
    if provider in {'postgresql', 'postgres'} and 'database' in route or provider == 'mysql' and 'schema' in route:
        raise ValueError('INVALID_DATABASE_PROVIDER_OPTION')
    return dict(route)


def _route_plugin(marker, name, definition):
    def mount(ctx, config):
        provider = _provider(definition)
        registry = ctx.require('databases.registry')
        dispose = registry.register(name, provider, owner_identity=ctx.identity, owner_lease=ctx.lease,
                                    registration_id=ctx.registration_id('database_route'))
        ctx.defer(dispose)
        ctx.provide(marker, MappingProxyType({'registration_id': registry.inspect()[name]['registration_id'],
                                              'owner_identity': tuple(sorted(dict(ctx.identity).items()))}))

    def inputs(config):
        return environment_inputs({value for key, value in definition.items() if key.endswith('_env')})

    return PluginSpec(marker, (marker,), ('databases.registry',), mount,
                      config_schema=config_schema(), config_inputs=inputs)


def _binding_plugin(key, marker, route, space, expected_id, consumer):
    def mount(ctx, config):
        registration = ctx.require(marker)
        registry = ctx.require('databases.registry')
        inspection = registry.inspect().get(route)
        if (inspection is None or inspection['registration_id'] != registration['registration_id'] or
                inspection['owner_identity'] != registration['owner_identity']):
            raise DatabaseError('DATABASE_ROUTE_OWNER_MISMATCH')
        grant = registry.grant(route, space, consumer, expected_dataset_id=expected_id)
        ctx.provide(key, registry.bind(grant, owner_lease=ctx.lease))

    return PluginSpec(key, (key,), ('databases.registry', marker), mount, config_schema=config_schema())


def _consumer_plugin(spec, key, mapping):
    frozen = MappingProxyType(dict(mapping))

    def mount(ctx, config):
        ctx._database_slots = frozen
        return spec.mount(ctx, config)

    return replace(spec, key=key, requires=tuple(dict.fromkeys((*spec.requires, *frozen.values()))), mount=mount)


def _key(prefix, *parts):
    return prefix + '.' + '.'.join(part.encode('utf-8').hex() for part in parts)


def _provider(definition):
    selected = dict(definition)
    provider = selected.pop('provider')
    for key in tuple(selected):
        if key.endswith('_env'):
            name = selected.pop(key)
            value = os.environ.get(name)
            if not value:
                raise DatabaseError('DATABASE_ENVIRONMENT_REQUIRED:' + name)
            selected[key[:-4]] = value
    if provider == 'sqlite':
        return SQLiteProvider(selected['root'])
    if provider in {'postgres', 'postgresql'}:
        return PostgresProvider(**selected)
    return MySQLProvider(**selected)

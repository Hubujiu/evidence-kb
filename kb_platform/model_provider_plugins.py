from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import httpx

from .model_configuration import configured_adapter, configured_budget, read_model_config
from .plugin_configuration import config_schema, environment_inputs, file_inputs, TEXT
from .plugin_host import PluginSpec
from .storage_sdk import StorageSlot
from .storage_sdk.relational import GUARDED_RELATIONAL_V1


def model_route_marker(route):
    if route in {'generation', 'embedding', 'ocr'}:
        return 'models.route.' + route
    return 'models.route.named.' + route.encode('utf-8').hex()


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                             allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _budget_inputs(config):
    value = read_model_config(config['config_path'])
    return {'model-budget:' + str(Path(config['config_path']).resolve()): _fingerprint(value['budget'])}


def _budget_check(config):
    read_model_config(config['config_path'])
    if 'database_slot' in config and 'audit_root' not in config:
        raise ValueError('MODEL_AUDIT_STORAGE_REQUIRED')


def model_resource_plugins():
    def budget(ctx, config):
        database = None
        if 'database_slot' in config:
            from .storage_plugins import require_storage

            database = require_storage(ctx, config['database_slot'])
        ledger = configured_budget(read_model_config(config['config_path']), database=database)
        ctx.defer(ledger.close)
        ctx.provide('models.budget', ledger)

    return (PluginSpec('models.budget', ('models.budget',), (), budget,
        config_schema=config_schema({'config_path': TEXT, 'database_slot': TEXT, 'audit_root': TEXT},
                                    required=('config_path',)),
        config_check=_budget_check, config_inputs=_budget_inputs),)


def _provider_plugin(key, provider, expected_routes, expected_dimensions):
    def selected(config):
        value = read_model_config(config['config_path'])
        routes = {name: row for name, row in value['models'].items() if row['provider'] == provider}
        dimensions = {name: row.get('dimension') for name, row in routes.items()}
        if provider not in value['providers'] or set(routes) != set(expected_routes) or dimensions != expected_dimensions:
            raise ValueError('MODEL_PROVIDER_GRAPH_RECOMPILE_REQUIRED')
        return value, routes

    def check(config):
        selected(config)

    def inputs(config):
        value, routes = selected(config)
        definition = value['providers'][provider]
        return {'model-provider:' + str(Path(config['config_path']).resolve()) + ':' + provider:
                _fingerprint({'provider': definition, 'routes': routes}),
                **environment_inputs({definition['api_key_env']})}

    def mount(ctx, config):
        value, routes = selected(config)
        ledger, registry = ctx.require('models.budget'), ctx.require('models')
        client = httpx.Client(follow_redirects=False, trust_env=False)
        ctx.defer(client.close)
        for name in routes:
            adapter = configured_adapter(value, name, ledger, client=client, audit_root=config.get('audit_root'))
            ctx.defer(adapter.close)
            remove = registry.register(name, adapter, registration_id=ctx.registration_id(kind='model_route'),
                owner_identity=ctx.identity, owner_lease=ctx.lease)
            ctx.defer(remove)
            ctx.provide(model_route_marker(name), ctx.identity)

    return PluginSpec(key, tuple(model_route_marker(name) for name in expected_routes),
        ('models', 'models.budget'), mount,
        config_schema=config_schema({'config_path': TEXT, 'audit_root': TEXT}, required=('config_path',)),
        config_check=check, config_inputs=inputs)


def _remote_plugin(key):
    def check(config):
        from .remote import read_provider_config

        read_provider_config(config['providers_config'], 'luna')

    def inputs(config):
        check(config)
        return file_inputs([config['providers_config']])

    def mount(ctx, config):
        from .model_adapters import LunaAdapter
        from .remote import LunaClient

        client = LunaClient(config['providers_config'])
        ctx.defer(client.close)
        remove = ctx.require('models').register('generation', LunaAdapter(client),
            registration_id=ctx.registration_id(kind='model_route'),
            owner_identity=ctx.identity, owner_lease=ctx.lease)
        ctx.defer(remove)
        ctx.provide('models.route.generation', ctx.identity)

    return PluginSpec(key, ('models.route.generation',), ('models',), mount,
        config_schema=config_schema({'providers_config': TEXT}, required=('providers_config',)),
        config_check=check, config_inputs=inputs)


def _generated_key(kind, identity):
    return 'models.' + kind + '.' + identity.encode('utf-8').hex()


def compile_model_plugins(profile, catalog):
    selected, specs = deepcopy(profile), list(catalog)
    additions, entries = [], []
    for row in selected['plugins']:
        if row.get('disabled') or row['use'] != 'models.service':
            continue
        config = row.get('config', {})
        if not set(config) & {'config_path', 'providers_config'}:
            continue
        if ('config_path' in config) == ('providers_config' in config) or set(config) - {
                'config_path', 'providers_config', 'database_slot', 'audit_root'}:
            raise ValueError('INVALID_MODELS_CONFIG')
        if 'providers_config' in config:
            if set(config) != {'providers_config'}:
                raise ValueError('INVALID_MODELS_CONFIG')
            key = _generated_key('provider', row['id'] + ':luna')
            spec = _remote_plugin(key)
            spec.config_check(config)
            additions.append(spec)
            entries.append({'id': key, 'use': key, 'config': deepcopy(config)})
            row['config'] = {'owned': True}
            continue
        value = read_model_config(config['config_path'])
        _budget_check(config)
        registry_id = row['id'] + '.registry'
        entries.append({'id': registry_id, 'use': 'models.service', 'config': {'owned': True}})
        identity = row['id']
        row['use'], row['config'] = 'models.budget', deepcopy(config)
        for provider in value['providers']:
            routes = {name: item for name, item in value['models'].items() if item['provider'] == provider}
            if not routes:
                continue
            key = _generated_key('provider', identity + ':' + provider)
            additions.append(_provider_plugin(key, provider, tuple(routes),
                {name: item.get('dimension') for name, item in routes.items()}))
            entries.append({'id': key, 'use': key,
                'config': {name: config[name] for name in ('config_path', 'audit_root') if name in config}})
    for row in selected['plugins']:
        if row.get('disabled') or row['use'] != 'models.budget' or 'database_slot' not in row.get('config', {}):
            continue
        key = _generated_key('budget', row['id'])
        base = next(spec for spec in specs if spec.key == 'models.budget')
        additions.append(replace(base, key=key,
            storage_slots=(StorageSlot(row['config']['database_slot'], GUARDED_RELATIONAL_V1),)))
        row['use'] = key
    selected['plugins'].extend(entries)
    by_spec = {spec.key: spec for spec in (*specs, *additions)}
    markers = {name for row in selected['plugins'] if not row.get('disabled')
               for name in by_spec[row['use']].provides if name.startswith('models.route.')}
    defaults = {'models.structured': 'generation', 'models.embedding': 'embedding', 'models.ocr': 'ocr',
                'knowledge.model': 'generation', 'knowledge.model.route': 'generation',
                'knowledge.reranker': 'generation', 'platform.research': 'generation',
                'platform.embedding.client': 'embedding', 'platform.ocr.client': 'generation'}
    for row in selected['plugins']:
        if row.get('disabled') or row['use'] not in defaults:
            continue
        spec = by_spec[row['use']]
        marker = model_route_marker(row.get('config', {}).get('route', defaults[spec.key]))
        if not markers:
            continue
        if marker not in markers:
            raise ValueError('MODEL_ROUTE_UNAVAILABLE')
        default_marker = model_route_marker(defaults[spec.key])
        requires = tuple(name for name in spec.requires if name != default_marker or marker == default_marker)
        requires = tuple(dict.fromkeys((*requires, marker)))
        if requires == spec.requires:
            continue
        key = _generated_key('consumer', row['id'])
        additions.append(replace(spec, key=key, requires=requires))
        row['use'] = key
    identities = [row['id'] for row in selected['plugins']]
    keys = [spec.key for spec in (*specs, *additions)]
    if len(identities) != len(set(identities)) or len(keys) != len(set(keys)):
        raise ValueError('MODEL_GENERATED_KEY_COLLISION')
    return selected, tuple((*specs, *additions))

import os
import json
from pathlib import Path

from .harness_application import HostedAgentApplication
from .harness_services import builtin_plugins
from .plugin_host import PluginHost
from .plugin_profiles import expand_profile


CONFIG = Path(__file__).resolve().parents[1] / 'config'
DEFAULT_PROFILE = CONFIG / 'agent-profiles/core.json'


def load_agent_profile(profile=None, *, environment=None, bundles=None):
    environment = os.environ if environment is None else environment
    catalog = {'core': CONFIG / 'agent-bundles/core.json', **(bundles or {})}
    selected = expand_profile(profile if profile is not None else DEFAULT_PROFILE, catalog)
    if environment.get('AGENT_CORE_ROOT'):
        for row in selected['plugins']:
            if row['use'] == 'harness.store.sqlite':
                row.setdefault('config', {})['root'] = environment['AGENT_CORE_ROOT']
    return selected


def create_agent_application(*, decide=None, model=None, tools=(), profile=None,
                             extra_plugins=(), bundles=None, environment=None, models=None,
                             storage_configuration=None):
    from dataclasses import replace
    from .storage_catalog import default_storage_providers
    from .storage_plugins import compile_storage_plugins
    from .storage_sdk import RELATIONAL_V1, StorageSlot
    from .model_plugins import model_plugins

    selected = load_agent_profile(profile, environment=environment, bundles=bundles)
    catalog = (*model_plugins(models), *builtin_plugins(decide=decide, model=model, tools=tools), *extra_plugins)
    environment = os.environ if environment is None else environment
    if environment.get('KB_DATABASES_CONFIG'):
        raise ValueError('LEGACY_STORAGE_CONFIGURATION_REQUIRES_CONVERSION')
    if storage_configuration is None and environment.get('KB_STORAGE_CONFIG'):
        storage_configuration = json.loads(Path(environment['KB_STORAGE_CONFIG']).read_text('utf-8'))
    if storage_configuration is not None:
        changes = {}
        for row in selected['plugins']:
            if row['id'] not in storage_configuration.get('bindings', {}):
                continue
            if row['use'] in ('harness.store.sqlite', 'harness.store.database'):
                row.update(use='harness.store.database', config={})
                changes[row['use']] = (StorageSlot('primary', RELATIONAL_V1),)
            elif row['use'] == 'models.service':
                row.setdefault('config', {})['database_slot'] = 'budget'
                changes[row['use']] = (StorageSlot('budget', RELATIONAL_V1),)
        catalog = tuple(replace(spec, storage_slots=changes[spec.key]) if spec.key in changes else spec
                        for spec in catalog)
        selected, catalog = compile_storage_plugins(selected, catalog, storage_configuration,
                                                     provider_catalog=default_storage_providers())
    host = PluginHost(selected, catalog)
    try:
        application = host.service('application')
        return HostedAgentApplication(host, application), host
    except BaseException:
        host.close()
        raise

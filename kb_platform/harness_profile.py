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
                             database_configuration=None):
    from .database_plugins import compile_database_plugins
    from .model_plugins import model_plugins

    selected = load_agent_profile(profile, environment=environment, bundles=bundles)
    catalog = (*model_plugins(models), *builtin_plugins(decide=decide, model=model, tools=tools), *extra_plugins)
    environment = os.environ if environment is None else environment
    if database_configuration is None and environment.get('KB_DATABASES_CONFIG'):
        database_configuration = json.loads(Path(environment['KB_DATABASES_CONFIG']).read_text('utf-8'))
    if database_configuration is not None:
        slots = {}
        for row in selected['plugins']:
            if row['id'] not in database_configuration.get('bindings', {}):
                continue
            if row['use'] in ('harness.store.sqlite', 'harness.store.database'):
                row.update(use='harness.store.database', config={})
                slots[row['use']] = ('primary',)
            elif row['use'] == 'models.service':
                row.setdefault('config', {})['database_slot'] = 'budget'
                slots[row['use']] = ('budget',)
        selected, catalog = compile_database_plugins(selected, catalog, database_configuration, slots=slots)
    host = PluginHost(selected, catalog)
    try:
        application = host.service('application')
        return HostedAgentApplication(host, application), host
    except BaseException:
        host.close()
        raise

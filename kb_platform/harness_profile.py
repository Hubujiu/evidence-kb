"""Opt-in generic Agent composition, without importing the knowledge product."""
import os
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
                             extra_plugins=(), bundles=None, environment=None):
    selected = load_agent_profile(profile, environment=environment, bundles=bundles)
    host = PluginHost(selected, (*builtin_plugins(decide=decide, model=model, tools=tools), *extra_plugins))
    try:
        application = host.service('application')
        return HostedAgentApplication(host, application), host
    except BaseException:
        host.close()
        raise

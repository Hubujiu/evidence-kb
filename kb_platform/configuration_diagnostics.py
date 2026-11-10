import hashlib
import json
import shutil


def configuration_diagnostics(composition, *, environment, explicit_profile=False, explicit_storage=False,
                              explicit_models=False):
    application = composition.application
    contracted = getattr(application, 'composition', None)
    plan = getattr(contracted, 'plan', None)
    nodes = []
    if isinstance(plan, dict):
        plugins = plan.get('plugins', [])
        selected = plugins.values() if isinstance(plugins, dict) else plugins
        for row in selected:
            if isinstance(row, dict):
                nodes.append({key: value for key, value in row.items()
                    if key in {'id', 'instance', 'implementation', 'version'} and type(value) in (str, int)})
    model = application.model_status() if application is not None else None
    inactive = ('KB_AGENT_PROFILE', 'KB_AGENT_PROVIDERS_CONFIG', 'KB_AGENT_ROOT') if contracted is not None else ()
    return {'application': {'kind': 'contract' if contracted is not None else 'profile',
            'graph_sha256': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
                if isinstance(plan, dict) else None, 'nodes': nodes},
        'configuration_sources': {
            'application': 'explicit' if explicit_profile else
                'KB_APPLICATION_PROFILE' if environment.get('KB_APPLICATION_PROFILE') else 'default',
            'storage': 'explicit' if explicit_storage else
                'KB_STORAGE_CONFIG' if environment.get('KB_STORAGE_CONFIG') else 'settings.storage_file',
            'models': 'explicit' if explicit_models else
                'KB_MODELS_CONFIG' if environment.get('KB_MODELS_CONFIG') else
                'KB_PROVIDERS_CONFIG' if environment.get('KB_PROVIDERS_CONFIG') else 'default'},
        'inactive_environment': [name for name in inactive if environment.get(name)],
        'model': {'configured': model is not None, 'provider_id': model.get('provider_id') if model else None},
        'development_tools': {name: shutil.which(name) is not None for name in ('node', 'npm', 'cargo', 'java')},
        'model_calls': 0, 'dependency_probe': 'not_run'}

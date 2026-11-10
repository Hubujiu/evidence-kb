import hashlib
import json
import shutil


def configuration_diagnostics(composition):
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
    provenance = composition.configuration_provenance
    return {'application': {'kind': 'contract' if contracted is not None else 'profile',
            'graph_sha256': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
                if isinstance(plan, dict) else None, 'nodes': nodes},
        'configuration_sources': dict(provenance['configuration_sources']),
        'inactive_environment': list(provenance['inactive_environment']),
        'model': {'configured': model is not None, 'provider_id': model.get('provider_id') if model else None},
        'development_tools': {name: shutil.which(name) is not None for name in ('node', 'npm', 'cargo', 'java')},
        'model_calls': 0, 'dependency_probe': 'not_run'}

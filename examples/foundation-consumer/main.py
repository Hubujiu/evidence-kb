import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

class LocalTextProvider:
    def __init__(self):
        from kb_platform.model_sdk import Capability, ModelInfo

        self.info = ModelInfo('local-example', 'literal-text-v1', frozenset({Capability.CHAT}))

    def invoke(self, request):
        from kb_platform.model_sdk import ModelResult

        return ModelResult(self.info.provider, self.info.model, request.capability,
            {'text': 'Local provider received: ' + request.payload['messages'][-1]['content'],
             'finish_reason': 'stop'}, request_id=uuid4().hex)

    def close(self):
        pass


def default_storage(root):
    from kb_platform.storage_catalog import initialize_configuration

    path = root / 'storage.json'
    if not path.exists():
        value = initialize_configuration({'version': 2, 'application_id': 'foundation-consumer-example',
            'instances': {
                'records': {'provider': 'sqlite', 'config': {'root': str(root / 'records')}},
                'objects': {'provider': 'files', 'config': {'root': str(root / 'objects')}}},
            'services': {
                'state': {'instance': 'records', 'export': 'kv'},
                'budget': {'instance': 'records', 'export': 'structured'},
                'objects': {'instance': 'objects', 'export': 'object'}},
            'bindings': {
                'notes': {
                    'state': {'service': 'state', 'owner': 'notes', 'space': 'state'},
                    'objects': {'service': 'objects', 'owner': 'notes', 'space': 'content'}},
                'provider': {'primary': {'service': 'budget', 'owner': 'provider-budget', 'space': 'physical'}}}})
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    return path


def notes(ctx, config):
    from kb_platform.application_storage import StorageClient
    from kb_platform.llm_contract import LlmClient
    from kb_platform.model_sdk import Capability, ModelRequest

    llm = LlmClient(ctx.require('llm'))
    storage = ctx.require('storage')
    state, objects = StorageClient(storage, 'notes.state'), StorageClient(storage, 'notes.objects')

    class Notes:
        def run(self, request):
            previous = state.get('last-reply')
            result = llm.invoke(config['route'], ModelRequest(Capability.CHAT,
                {'messages': [{'role': 'user', 'content': request['message']}], 'max_tokens': 128},
                {'purpose': 'foundation-example'}))
            content = result.data['text'].encode('utf-8')
            reference = objects.put('example-tenant', content)
            recovered = objects.read('example-tenant', reference.locator, sha256=reference.sha256)
            if recovered != content:
                raise ValueError('FOUNDATION_OBJECT_ROUNDTRIP_FAILED')
            state.set('last-reply', recovered)
            if state.get('last-reply') != recovered:
                raise ValueError('FOUNDATION_KV_ROUNDTRIP_FAILED')
            return {'protocol': 'foundation-consumer/1', 'mode': config['mode'],
                'provider': result.provider, 'model': result.model, 'reply': result.data['text'],
                'previous_reply': previous.decode('utf-8') if previous is not None else None,
                'usage': result.normalized_usage, 'finish': result.normalized_finish,
                'object': {'locator': reference.locator, 'sha256': reference.sha256, 'size': reference.size},
                'storage_identity': asdict(objects.identity)}

    ctx.provide('application', Notes())


def run(root, *, models_config=None, storage_config=None, route='generation', message='Hello, foundation.'):
    from kb_platform.foundation_services import build_foundation_application
    from kb_platform.model_service import ModelService
    from kb_platform.persistence_schema import budget_schema
    from kb_platform.plugin_configuration import TEXT, config_schema
    from kb_platform.plugin_contracts import Method
    from kb_platform.storage_sdk import KV_DURABLE_V1, OBJECT_V1
    from kb_platform.storage_sdk.relational import GUARDED_RELATIONAL_V1

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    storage_path = Path(storage_config).resolve() if storage_config else default_storage(root)
    llm_config = {}
    models = None
    if models_config:
        llm_config = {'config_path': str(Path(models_config).resolve()), 'budget_binding': 'provider.primary',
                      'audit_root': str(root / 'model-audit')}
    else:
        models = ModelService()
        models.register(route, LocalTextProvider(), owned=True)
    profile = {'version': 1, 'plugins': {
        'storage': {'implementation': 'foundation.storage', 'version': '1', 'config': {'path': str(storage_path)}},
        'llm': {'implementation': 'foundation.llm', 'version': '1', 'config': llm_config},
        'notes': {'implementation': 'example.notes', 'version': '1', 'config': {
            'route': route, 'mode': 'configured-provider' if models_config else 'local-provider'}}},
        'bindings': {
            'llm': {'storage': {'plugin': 'storage', 'export': 'storage'}},
            'notes': {'llm': {'plugin': 'llm', 'export': 'llm'},
                      'storage': {'plugin': 'storage', 'export': 'storage'}}},
        'entrypoint': {'plugin': 'notes', 'export': 'application'}}
    consumer = {'implementation': 'example.notes', 'version': '1', 'executor': 'local',
        'provides': {'application': {'id': 'application.run', 'major': 1}},
        'requires': {'llm': {'contract': {'id': 'llm.service', 'major': 1}},
                     'storage': {'contract': {'id': 'storage.service', 'major': 1}}}}
    application = None
    try:
        application = build_foundation_application(profile, models=models,
            storage_requirements={'notes': {'state': KV_DURABLE_V1, 'objects': OBJECT_V1},
                                  'provider': {'primary': GUARDED_RELATIONAL_V1}},
            storage_schemas={'provider.primary': budget_schema}, extra_catalog=(consumer,),
            extra_mounts={('example.notes', '1'): notes},
            schemas={('example.notes', '1'): config_schema({'route': TEXT, 'mode': TEXT},
                                                         required=('route', 'mode'))},
            contracts={('application.run', 1): {'run': Method(
                config_schema({'message': TEXT}, required=('message',)), {'type': 'object'})}})
        report = application.run({'message': message})
        report['implementations'] = sorted(row['implementation'] for row in application.plan['plugins'])
        return report
    finally:
        try:
            if application is not None:
                application.close()
        finally:
            if models is not None:
                models.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Use the LLM and storage foundation without the knowledge application.')
    parser.add_argument('--root', type=Path, default=Path('.local/foundation-consumer'))
    parser.add_argument('--models-config', type=Path)
    parser.add_argument('--storage-config', type=Path)
    parser.add_argument('--route', default='generation')
    parser.add_argument('--message', default='Hello, foundation.')
    arguments = parser.parse_args(argv)
    report = run(arguments.root, models_config=arguments.models_config, storage_config=arguments.storage_config,
                 route=arguments.route, message=arguments.message)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

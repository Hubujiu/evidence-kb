from copy import deepcopy
import json
import os
from pathlib import Path

from kb_platform.embeddings import EmbeddingError
from kb_platform.host_applications import HttpApplication
from kb_platform.plugin_host import PluginSpec


class DeterministicEmbedding:
    provider_id = 'storage_fixture'
    dimension = 3
    manifest = {'provider': provider_id, 'model': 'deterministic-synthetic-storage-v1', 'dimension': 3}

    def __init__(self, directory):
        self.directory, self.calls = Path(directory), []

    def documents(self, texts):
        if (self.directory / 'embedding.fail').exists():
            raise EmbeddingError('SYNTHETIC_LOCAL_EMBEDDING_FAILURE')
        self.calls.append({'kind': 'documents', 'items': len(texts)})
        return [[1.0, 0.25, 0.125] for _ in texts]

    def query(self, text):
        self.calls.append({'kind': 'query', 'items': 1})
        return [1.0, 0.25, 0.125]


class RecordingModelSink:
    def __init__(self):
        self.received = []

    def complete(self, messages):
        self.received.append(deepcopy(messages))
        return {'status': 'recorded', 'scope': 'deterministic recording sink; no answer generation'}


def api(config):
    from kb_platform.api import create_app

    provider = DeterministicEmbedding(config['directory'])

    def capabilities(ctx, values):
        ctx.provide('platform.embedding', provider)
        ctx.provide('platform.ocr', None)

    app = create_app(profile=config['application_profile'], extra_plugins=(
        PluginSpec('storage.fixture.capabilities', ('platform.embedding', 'platform.ocr'), (), capabilities),))
    application = HttpApplication(app, config)
    original_close = application.close

    def close():
        try:
            original_close()
        finally:
            document = {'local_fixture_calls': deepcopy(provider.calls),
                        'new_model_calls': 0, 'new_embedding_provider_calls': 0,
                        'scope': 'local deterministic vectors; no model or external embedding service'}
            (provider.directory / ('local-fixture-calls-' + str(os.getpid()) + '.json')).write_text(
                json.dumps(document, ensure_ascii=False, indent=2), 'utf-8')

    application.close = close
    return application

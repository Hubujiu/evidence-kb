from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .core import PlatformError
from .embeddings import EmbeddingError
from .google_semantic_provider import GoogleSemanticProvider
from .remote import RemoteModelError
from .rag_testing.budget import BudgetLedger
from .rag_testing.contracts import digest
from .semantic_pages_v2 import SemanticPageService

ROOT = Path(__file__).resolve().parents[1]


def _path(value):
    if not isinstance(value, str) or not value:
        raise ValueError('An explicit runtime path is required')
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def from_config(platform, gateway, config_path, *, provider_factories=None):

    provider = None
    try:
        path = _path(str(config_path))
        raw = path.read_bytes()
        config = json.loads(raw)
        common = {'schema', 'provider_id', 'storage_root', 'ledger_path', 'audit_dir', 'caps',
                  'expected_provider_manifest_sha256'}
        if not isinstance(config, dict):
            raise ValueError('Invalid semantic runtime configuration')
        remote = True
        required = common | {'provider_config'}
        if (set(config) != required or config['schema'] != 'semantic-page-runtime/2'
                or config['provider_id'] != 'google_gemini'):
            raise ValueError('Invalid semantic runtime configuration')
        caps = config['caps']
        call_resource = 'remote_embedding_calls'
        if (not isinstance(caps, dict) or set(caps) != {call_resource, 'input_tokens'} or
                any(type(v) is not int or v < 1 for v in caps.values())):
            raise ValueError('Finite physical query call and input reservations are required')
        ledger = BudgetLedger(_path(config['ledger_path']), caps)
        factories = {'google_gemini': GoogleSemanticProvider} if provider_factories is None else provider_factories
        factory = factories.get(config['provider_id'])
        if factory is None:
            raise ValueError('The explicitly selected provider adapter is not configured')
        provider = factory(_path(config['provider_config']), ledger=ledger, audit_dir=_path(config['audit_dir']))
        if provider.provider_id != config['provider_id']:
            raise ValueError('Provider adapter identity differs from its admission')
        if digest(provider.manifest) != config['expected_provider_manifest_sha256']:
            raise ValueError('Configured provider differs from its admitted manifest')
        service = SemanticPageService(platform, provider, _path(config['storage_root']), gateway=gateway)
        service.runtime_identity = {'config_path': str(path), 'config_sha256': hashlib.sha256(raw).hexdigest(),
            'provider_manifest_sha256': digest(provider.manifest), 'query_caps': caps,
            'new_document_embeddings': 0, 'remote_embeddings': remote}
        return service
    except (OSError, ValueError, TypeError, KeyError, EmbeddingError, RemoteModelError) as exc:
        if provider is not None and callable(getattr(provider, 'close', None)):
            provider.close()
        raise PlatformError('SEMANTIC_PAGE_RUNTIME_INVALID', 'Configure the fixed Google semantic Page runtime', 409) from exc


def from_environment(platform, gateway):
    path = os.getenv('KB_SEMANTIC_PAGE_CONFIG')
    return from_config(platform, gateway, path) if path else None

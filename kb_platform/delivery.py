from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock

import httpx

from kb_platform.rag_testing.budget import BudgetLedger as PhysicalBudgetLedger
from kb_platform.rag_testing.budget_batches import _snapshot as snapshot_budget
from kb_platform.rag_testing.budget_batches import freeze_predecessor
from kb_platform.rag_testing.budget_batches import ReviewedPredecessorLedger, validate_predecessor
from kb_platform.rag_testing.provider_capture import CapturedLuna, observe_client, request_context
from kb_platform.rag_testing.contracts import digest
from kb_platform.embeddings import EmbeddingError
from kb_platform.remote import RemoteModelError
from kb_platform.settings import Settings
from kb_platform.provider_admission import CapturePreflightError


ROOT = Path(__file__).resolve().parents[1]

CAP_DIMENSIONS = {'model', 'input_tokens', 'output_tokens', 'remote_embedding_calls', 'embedding_input_tokens'}
WINDOW = {'total': 131072, 'input': 118784, 'output': 4096, 'safety': 8192,
          'counting': 'conservative_utf8_estimate'}
RUNTIME_CAPS = {'model': 120, 'input_tokens': 1500000, 'output_tokens': 200000,
                'remote_embedding_calls': 32, 'embedding_input_tokens': 60000}
RECOVERY_READ_ROUTES = frozenset({'/health', '/identity', '/capabilities', '/diagnostics', '/knowledge-bases', '/scopes',
    '/knowledge-bases/{kb_id}/documents', '/knowledge-bases/{kb_id}/sources/{source_id}/versions',
    '/knowledge-bases/{kb_id}/builds/{build_id}', '/knowledge-bases/{kb_id}/citations/{citation_id}',
    '/knowledge-bases/{kb_id}/citations/{citation_id}/original',
    '/knowledge-bases/{kb_id}/citations/{citation_id}/preview',
    '/agent/capabilities', '/agent/runtime-status', '/agent/readiness', '/agent/sessions',
    '/scopes/{scope_id}/agent-sources', '/scopes/{scope_id}/agent-authority',
    '/agent/sessions/{session_id}', '/agent/sessions/{session_id}/history',
    '/agent/sessions/{session_id}/export', '/agent/sessions/{session_id}/artifacts/{artifact_id}',
    '/agent/sessions/{session_id}/requests/{request_id}'})


def _deny_recovery_send(*args, **kwargs):
    raise CapturePreflightError('DELIVERY_RECOVERY_REQUIRED')


class RecoveryTransport(httpx.BaseTransport):
    def handle_request(self, request):
        return _deny_recovery_send()


def _install_recovery_guard(app, require_actor):
    from fastapi import Depends
    from fastapi.dependencies.utils import get_dependant
    from fastapi.responses import JSONResponse
    from fastapi.routing import APIRoute

    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    status_route = next((route for route in routes if route.path == '/agent/runtime-status'), None)
    identity = next((dependency.call for dependency in status_route.dependant.dependencies
                     if dependency.name == 'actor'), None) if status_route is not None else None
    if identity is None:
        raise DeliveryConfigurationError('DELIVERY_RECOVERY_AUTHENTICATION_UNAVAILABLE')

    class RecoveryRequired(Exception):
        pass

    @app.exception_handler(RecoveryRequired)
    async def recovery_error(request, exc):
        return JSONResponse(status_code=503, content={'error': 'DELIVERY_RECOVERY_REQUIRED',
            'mode': 'restricted_recovery', 'reason': 'UNRESOLVED_PHYSICAL_REQUEST',
            'model_sent': False, 'automatic_retry': False,
            'allowed_actions': ['inspect_request', 'inspect_history', 'export_session', 'reconcile_ledger']})

    def require_recovery_read(actor=Depends(identity)):
        require_actor(actor)
        raise RecoveryRequired()

    for route in routes:
        if route.path not in RECOVERY_READ_ROUTES or not route.methods.issubset({'GET', 'HEAD'}):
            route.dependant.dependencies.insert(0, get_dependant(path=route.path, call=require_recovery_read))


def validate_runtime_caps(caps):
    if (not isinstance(caps, dict) or set(caps) != CAP_DIMENSIONS or
            any(type(value) is not int or value < 1 for value in caps.values())):
        raise ValueError('INVALID_RUNTIME_BUDGET_CAPS')
    return caps


class DeliveryConfigurationError(ValueError):
    pass


def _path(value):
    result = Path(value)
    return result.resolve() if result.is_absolute() else (ROOT / result).resolve()


@dataclass(frozen=True)
class DeliveryProfile:
    config_path: Path
    provider_path: Path
    root: Path
    config: dict

    @property
    def identities_file(self):
        return self.root / 'identities.json'

    def public(self):
        return {'protocol': self.config['protocol'], 'id': self.config['id'], 'port': 58089,
                'models': deepcopy(self.config['models']), 'window': deepcopy(self.config['window']),
                'physical_call_caps': deepcopy(self.config['physical_call_caps']),
                'root': str(self.root), 'identities_file': str(self.identities_file),
                'providers_file': str(self.provider_path), 'cross_session_memory': False}


def load_profile(config_path=None, providers_path=None, run_root=None):
    config_path = _path(config_path or os.getenv('KB_DELIVERY_CONFIG', 'config/delivery-local-v1.json'))
    try:
        config = json.loads(config_path.read_text('utf-8'))
        if (config.get('protocol') not in {'enterprise-kb-delivery/1', 'enterprise-kb-delivery/2'}
                or not re.fullmatch(r'delivery-[a-zA-Z0-9-]{1,100}', config.get('id', ''))
                or config.get('window') != WINDOW
                or config.get('scope', {}).get('cross_session_memory') is not False
                or config.get('admission', {}).get('concurrent_model_requests') != 1):
            raise DeliveryConfigurationError('INVALID_FIXED_DELIVERY_PROFILE')
        caps = config['physical_call_caps']
        if set(caps) != CAP_DIMENSIONS or any(type(v) is not int or v <= 0 for v in caps.values()):
            raise DeliveryConfigurationError('INVALID_DELIVERY_PHYSICAL_CAPS')
        root = _path(run_root or os.getenv('KB_DELIVERY_ROOT', '.local/' + config['id']))
        provider_path = _path(providers_path or os.getenv('KB_DELIVERY_PROVIDERS', str(root / 'providers.json')))
        selected = json.loads(provider_path.read_text('utf-8'))
        luna, google = selected['luna'], selected['google']
        expected_embedding = f"google_gemini:{google['model']}:{google['dimension']}"
        generation = config['models'].get('generation')
        if (not isinstance(generation, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', generation)
                or (config['protocol'] == 'enterprise-kb-delivery/1' and generation != 'codex/gpt-6-luna')
                or luna.get('model') != generation
                or config['models'].get('embedding') != expected_embedding
                or google.get('model') != 'gemini-embedding-2' or google.get('dimension') != 768
                or config['models'].get('model_comparison') is not False):
            raise DeliveryConfigurationError('DELIVERY_PROVIDER_IDENTITY_MISMATCH')
        for section in (luna, google):
            if section.get('cache_mode') != 'disabled' or section.get('retries') != 0:
                raise DeliveryConfigurationError('DELIVERY_REQUIRES_EXPLICIT_UNCACHED_NO_RETRY')
        if not provider_path.is_relative_to(root):
            raise DeliveryConfigurationError('DELIVERY_PROVIDERS_MUST_BE_PRIVATE_TO_RUN')
        for name in ('cache_dir', 'log_path'):
            if not _path(selected[name]).is_relative_to(root):
                raise DeliveryConfigurationError('DELIVERY_OUTPUT_MUST_NOT_REUSE_HISTORICAL_PATHS')
        return DeliveryProfile(config_path, provider_path, root, config)
    except DeliveryConfigurationError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise DeliveryConfigurationError('DELIVERY_CONFIGURATION_UNAVAILABLE') from None


def ensure_private_identity(profile):

    profile.root.mkdir(parents=True, exist_ok=True)
    path = profile.identities_file
    if not path.exists():
        identities = {secrets.token_urlsafe(36): {'tenant_id': profile.config['id'], 'principal_id': 'local'}}
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(identities, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
    try:
        identities = json.loads(path.read_text('utf-8'))
        if (not isinstance(identities, dict) or not identities or any(
                not isinstance(token, str) or len(token) < 32 or not isinstance(actor, dict)
                or actor != {'tenant_id': profile.config['id'], 'principal_id': 'local'}
                for token, actor in identities.items())):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise DeliveryConfigurationError('DELIVERY_IDENTITY_INVALID_DO_NOT_OVERWRITE') from None
    return path


def configure_environment(profile, active_caps=None):

    caps = active_caps or profile.config['physical_call_caps']
    values = {'KB_REMOTE_MODELS': '1', 'KB_ENABLE_EMBEDDINGS': '0', 'KB_SCOPE_ONLY': '0',
        'KB_DEFER_DEFAULT_APP': '1', 'KB_PROVIDERS_CONFIG': str(profile.provider_path),
        'KB_AGENT_PROVIDERS_CONFIG': str(profile.provider_path),
        'KB_STORAGE_ROOT': str(profile.root / 'originals'), 'KB_IDENTITIES_FILE': str(profile.identities_file),
        'KB_RESEARCH_ROOT': str(profile.root / 'research'), 'KB_AGENT_ROOT': str(profile.root / 'sessions'),
        'KB_AGENT_INPUT_BUDGET': str(WINDOW['input']), 'KB_AGENT_OUTPUT_RESERVE': str(WINDOW['output']),
        'KB_AGENT_MODEL_WINDOW': str(WINDOW['total']), 'KB_AGENT_SAFETY_RESERVE': str(WINDOW['safety']),
        'KB_AGENT_MAX_CALLS': str(caps['model']),
        'KB_AGENT_MAX_TOKENS': str(caps['input_tokens'] + caps['output_tokens']),
        'KB_MAX_UPLOAD_BYTES': '67108864', 'EVIDENCE_KB_PARSE_ROUTE': 'auto',
        'EVIDENCE_KB_LAYOUT_BACKEND': 'docling', 'EVIDENCE_KB_PARSE_TIMEOUT_SECONDS': '1800',
        'EVIDENCE_KB_EXTERNAL_PARSE_TIMEOUT_SECONDS': '1740', 'PYTHONIOENCODING': 'utf-8'}
    os.environ.update(values)
    return Settings.from_environment()


def load_budget_batch(profile, path):

    if path is None:
        return None
    path = _path(path)
    try:
        value = json.loads(path.read_text('utf-8'))
        if (value.get('protocol') != 'delivery-runtime-budget-batch/1'
                or value.get('tenant_id') != profile.config['id']
                or not re.fullmatch(r'runtime-[a-zA-Z0-9-]{1,80}', value.get('id', ''))):
            raise ValueError()
        validate_runtime_caps(value.get('caps'))
        predecessors = value['predecessors']
        paths = [str(_path(parent['path'])) for parent in predecessors]
        original = str(profile.root / 'physical-calls.sqlite3')
        semantic = str(_path(value['semantic_predecessor']))
        if (original not in paths or semantic not in paths or original == semantic or
                len(set(paths)) != len(paths) or len(paths) < 2 or path.parent == profile.root):
            raise ValueError()
        for parent in predecessors:
            observed = validate_predecessor(parent)
            if observed['snapshot_sha256'] != parent['snapshot_sha256']:
                raise ValueError()
        ledger = path.parent / 'physical-calls.sqlite3'
        if str(ledger) in paths:
            raise ValueError()
        if ledger.exists():
            child = snapshot_budget(ledger)
            if child['caps'] != value['caps'] or child['summary']['over_budget']:
                raise ValueError()
        return {**value, 'manifest_path': str(path), 'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'ledger_path': str(ledger), 'capture_root': str(path.parent / 'physical-requests')}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise DeliveryConfigurationError('INVALID_OR_UNSETTLED_RUNTIME_BUDGET_BATCH') from None


def validate_semantic_runtime(config_path, *, provider_factory=None):

    provider = None
    try:
        path = _path(config_path)
        raw = path.read_bytes()
        config = json.loads(raw)
        if (set(config) != {'schema', 'provider_id', 'provider_config', 'storage_root', 'ledger_path',
                            'audit_dir', 'caps', 'expected_provider_manifest_sha256'} or
                config['schema'] != 'semantic-page-runtime/2' or config['provider_id'] != 'google_gemini' or
                set(config['caps']) != {'remote_embedding_calls', 'input_tokens'} or
                any(type(value) is not int or value < 1 for value in config['caps'].values())):
            raise ValueError()
        snapshot = freeze_predecessor(_path(config['ledger_path']))
        _path(config['storage_root'])
        if snapshot['caps'] != config['caps'] or snapshot['summary']['pending_or_unknown']:
            raise ValueError()

        class ReadOnlyLedger(PhysicalBudgetLedger):
            def __init__(self):
                self.path, self.caps = _path(config['ledger_path']), config['caps']

            def reserve(self, *args, **kwargs):
                raise DeliveryConfigurationError('SEMANTIC_PREFLIGHT_CANNOT_RESERVE')

            def finish(self, *args, **kwargs):
                raise DeliveryConfigurationError('SEMANTIC_PREFLIGHT_CANNOT_FINISH')

            def summary(self):
                return snapshot['summary']

        if provider_factory is None:
            from kb_platform.google_semantic_provider import GoogleSemanticProvider

            provider_factory = GoogleSemanticProvider
        provider = provider_factory(_path(config['provider_config']), ledger=ReadOnlyLedger(),
                                    audit_dir=_path(config['audit_dir']))
        actual = digest(provider.manifest)
        if provider.provider_id != config['provider_id'] or actual != config['expected_provider_manifest_sha256']:
            raise ValueError()
        return {'provider_id': provider.provider_id, 'provider_manifest_sha256': actual,
                'config_sha256': hashlib.sha256(raw).hexdigest(), 'ledger_access': 'READ_ONLY', 'new_requests': 0}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, EmbeddingError, RemoteModelError):
        raise DeliveryConfigurationError('SEMANTIC_PAGE_RUNTIME_INVALID') from None
    finally:
        if provider is not None:
            provider.close()


def _dependency_probe(platform, actor):
    result = {}
    try:
        scopes = platform.list_kbs(actor)
        result['repository'] = {'ready': True, 'visible_scopes': len(scopes)}
    except Exception as exc:
        return {'repository': {'ready': False, 'error': type(exc).__name__}}
    try:
        checked = 0
        for scope in scopes:
            if scope['manifest_id'] is not None:
                found = platform.search(actor, scope['kb_id'], 'delivery-readiness',
                                         channel='bm25', top_k=1, context_budget=1)
                if found['manifest_id'] != scope['manifest_id']:
                    raise ValueError('PUBLISHED_GENERATION_CHANGED')
                checked += 1
        result['search'] = {'ready': True, 'verified_published_generations': checked}
    except Exception as exc:
        result['search'] = {'ready': False, 'error': type(exc).__name__}
    return result


def _serialize_client_sends(provider, lock):


    send = provider.client.send

    from .agent_runtime import serialized_send as admitted_send

    def serialized_send(*args, **kwargs):
        return admitted_send(send, lock, *args, **kwargs)
    provider.client.send = serialized_send


def _budget_status(ledger, *, provider_blocked=False):
    snapshot = ledger.status_snapshot()
    usage, cumulative = snapshot['current'], snapshot['cumulative']
    totals = None if usage is None else usage['effective_usage_or_reserved']
    cumulative_totals = None if cumulative is None else cumulative['effective_usage_or_reserved']
    remaining = None if totals is None else {key: max(0, ledger.caps[key] - used)
                                           for key, used in totals.items()}
    model_exhausted = remaining is not None and any(
        remaining[key] == 0 for key in ('model', 'input_tokens', 'output_tokens'))
    budget_blocked = snapshot['blocked']
    budget_status = 'BLOCKED' if budget_blocked else 'BUDGET_EXHAUSTED' if model_exhausted else 'AVAILABLE'
    return {'status': 'BLOCKED' if provider_blocked else budget_status, 'budget_status': budget_status,
        'caps': deepcopy(ledger.caps), 'usage_available': usage is not None,
        'cumulative_usage_available': cumulative_totals is not None,
        'effective_usage_or_reserved': totals, 'remaining': remaining,
        'current_pending_or_unknown': None if usage is None else usage['pending_or_unknown'],
        'pending_or_unknown': None if cumulative is None else cumulative['pending_or_unknown'],
        'unknown_usage_requests': None if usage is None else usage['unknown_usage_requests'],
        'reviewed_unknown_requests': snapshot['reviewed_unknown_requests'],
        'cumulative_unknown_usage_requests': None if cumulative is None else cumulative['unknown_usage_requests'],
        'blocked_reason': snapshot['blocked_reason'] or (
            'UNRESOLVED_PHYSICAL_REQUEST' if snapshot['admission_pending_or_unknown'] else
            'PROVIDER_PERMISSION_BLOCKED' if provider_blocked else
            'BUDGET_EXHAUSTED' if model_exhausted else None),
        'cumulative_effective_usage_or_reserved': cumulative_totals}


def create_delivery_app(config_path=None, providers_path=None, run_root=None, *,
                        app_factory=None, inner_factory=None, budget_batch=None, dependency_probe=None):
    with ExitStack() as cleanup:
        app = _create_delivery_app(config_path, providers_path, run_root, app_factory=app_factory,
                                   dependency_probe=dependency_probe, budget_batch=budget_batch,
                                   inner_factory=inner_factory, cleanup=cleanup)
        resources = cleanup.pop_all()
        app.router.add_event_handler('shutdown', resources.close)
        return app


def _create_delivery_app(config_path, providers_path, run_root, *, app_factory,
                         dependency_probe, budget_batch, inner_factory, cleanup):
    instance = os.getenv('KB_DELIVERY_INSTANCE')
    if instance and not re.fullmatch('[0-9a-f]{64}', instance):
        raise DeliveryConfigurationError('INVALID_DELIVERY_INSTANCE')
    profile = load_profile(config_path, providers_path, run_root)
    batch = load_budget_batch(profile, budget_batch or os.getenv('KB_DELIVERY_BUDGET_BATCH') or None)
    ensure_private_identity(profile)
    settings = configure_environment(profile, batch['caps'] if batch else None)
    recovery_required = bool(batch and Path(batch['ledger_path']).exists()
                             and snapshot_budget(batch['ledger_path'])['summary']['pending_or_unknown'])
    ledger = (ReviewedPredecessorLedger(batch['ledger_path'], batch['caps'], predecessors=batch['predecessors'],
              allow_unresolved_child=recovery_required) if batch else
              PhysicalBudgetLedger(profile.root / 'physical-calls.sqlite3', profile.config['physical_call_caps']))
    capture_root = Path(batch['capture_root']) if batch else profile.root / 'physical-requests'
    recovery_required = recovery_required or bool(ledger.summary()['pending_or_unknown'])
    public_application = app_factory is None
    if public_application:
        from kb_platform.api import create_app

        app_factory = create_app
    from .remote import GoogleEmbeddingProvider
    from .platform_plugins import OwnedModelClients

    if os.getenv('KB_MODELS_CONFIG'):
        raise DeliveryConfigurationError('DELIVERY_REQUIRES_FROZEN_PROVIDER_CONFIG')
    captured = CapturedLuna(profile.provider_path)
    try:
        embedding = GoogleEmbeddingProvider(profile.provider_path)
    except BaseException:
        captured.close()
        raise
    owned_clients = OwnedModelClients(captured, embedding)
    cleanup.callback(owned_clients.close_unclaimed)
    providers, seen, lock = [], set(), Lock()
    semantic_configured = False

    def observe(provider, category):
        if id(provider) in seen:
            return
        seen.add(id(provider))
        inner = (RecoveryTransport() if recovery_required else
                 inner_factory(provider, category) if inner_factory is not None else None)
        capture_category = f'runtime:{batch["id"]}:{category}' if batch else category
        observe_client(provider, ledger, capture_root, category=capture_category, inner=inner)
        if recovery_required:
            provider.client.send = _deny_recovery_send
        else:
            _serialize_client_sends(provider, lock)
        providers.append(provider)

    def observe_semantic(service):
        nonlocal semantic_configured
        semantic_provider = getattr(service, 'provider', None)
        remote = getattr(semantic_provider, 'remote', None)
        if (getattr(semantic_provider, 'provider_id', None) != 'google_gemini' or remote is None
                or remote.model != 'gemini-embedding-2' or remote.dimension != 768):
            raise DeliveryConfigurationError('DELIVERY_SEMANTIC_PROVIDER_MISMATCH')
        observe(remote, 'delivery_semantic_embedding')
        semantic_configured = True

    observe(captured, 'delivery_model')
    observe(embedding, 'delivery_embedding')
    if public_application:
        app = app_factory(settings, model_clients=owned_clients,
                          workflow_options={'tool_context': request_context}, semantic_observer=observe_semantic)
    else:
        app = app_factory(settings, model_clients=owned_clients)
    if getattr(app.state, 'root_plugins', None) is not None:
        cleanup.callback(app.state.root_plugins.close)
    research, agent, platform = app.state.research, app.state.agent, app.state.platform
    if research is None or agent is None:
        raise DeliveryConfigurationError('DELIVERY_PROVIDER_WIRING_INVALID')
    if public_application:
        configured = platform.embedding_provider
        if (not research.configured or configured is None
                or configured.provider_id != embedding.provider_id or configured.manifest != embedding.manifest):
            raise DeliveryConfigurationError('DELIVERY_PROVIDER_WIRING_INVALID')
        policy = agent.model_status()['policy']
        dimensions = tuple(policy[name] for name in ('model_window', 'input_budget', 'output_reserve', 'safety_reserve'))
    else:
        if platform.embedding_provider is not research.embedding:
            raise DeliveryConfigurationError('DELIVERY_PROVIDER_WIRING_INVALID')
        policy = agent.model.policy
        dimensions = policy.model_window, policy.input_budget, policy.output_reserve, policy.safety_reserve
        agent.tool_context = request_context
    if dimensions != (
            WINDOW['total'], WINDOW['input'], WINDOW['output'], WINDOW['safety']):
        raise DeliveryConfigurationError('DELIVERY_AGENT_WINDOW_MISMATCH')
    if not public_application:
        semantic = getattr(agent, 'semantic_pages', None)
        if semantic is not None:
            observe_semantic(semantic)
    app.state.delivery = {'profile': profile.public(), 'ledger': ledger,
                          'providers': providers, 'model': captured, 'budget_batch': batch,
                          'capture_root': capture_root, 'recovery_required': recovery_required}

    def require_delivery_actor(actor):
        if actor.tenant_id != profile.config['id'] or actor.principal_id != 'local':
            from fastapi import HTTPException

            raise HTTPException(403, 'Delivery status is available only to this local identity')

    def status(actor):
        require_delivery_actor(actor)
        provider_channels = []
        for provider in providers:
            try:
                channel = provider.admission.snapshot(public=True)
            except (OSError, ValueError, KeyError, TypeError):
                channel = {'provider_id': provider.provider_id, 'model': provider.model,
                           'state': 'BLOCKED', 'reason': 'ADMISSION_STATE_UNAVAILABLE',
                           'automatic_probe': False}
            if channel not in provider_channels:
                provider_channels.append(channel)
        provider_blocked = any(channel['state'] == 'BLOCKED' for channel in provider_channels)
        budget = _budget_status(ledger, provider_blocked=provider_blocked)
        if recovery_required:
            budget.update(status='BLOCKED', blocked_reason=budget['blocked_reason'] or 'UNRESOLVED_PHYSICAL_REQUEST')
        return {'configured': True, 'profile': profile.config['id'],
            'mode': 'restricted_recovery' if recovery_required else 'runtime_batch' if batch else 'delivery',
            'recovery_required': recovery_required,
            'recovery_reason': 'UNRESOLVED_PHYSICAL_REQUEST' if recovery_required else None,
            'new_paid_requests_allowed': not recovery_required and budget['status'] == 'AVAILABLE',
            'mutations_allowed': not recovery_required,
            'recovery_requires_restart': recovery_required,
            'batch_id': batch['id'] if batch else None,
            **budget,
            'provider_blocked': provider_blocked, 'provider_channels': provider_channels,
            'scope': 'single local delivery identity; independent phase caps; unknown reservations retained',
            'model_calls_from_status': 0}

    def readiness(actor):
        require_delivery_actor(actor)
        dependencies = dependency_probe(settings) if dependency_probe else _dependency_probe(platform, actor)
        reads_ready = bool(dependencies) and all(value['ready'] for value in dependencies.values())
        return {'ready': reads_ready and not recovery_required, 'read_only_ready': reads_ready,
                'recovery_required': recovery_required,
                'dependencies': dependencies, 'process_alive': True, 'model_calls': 0,
                'scope': 'Mounted repository and visible published generations; empty search backends and answer quality not asserted'}

    app.state.delivery_status, app.state.delivery_readiness = status, readiness
    if recovery_required:
        _install_recovery_guard(app, require_delivery_actor)

    @app.middleware('http')
    async def identify_delivery_response(request, call_next):
        response = await call_next(request)
        response.headers['X-Delivery-Profile'] = profile.config['id']
        if instance and request.url.path == '/health':
            response.headers['X-Delivery-Instance'] = instance
        return response

    manifest = {**profile.public(), 'config_sha256': hashlib.sha256(profile.config_path.read_bytes()).hexdigest(),
                'provider_config_sha256': hashlib.sha256(profile.provider_path.read_bytes()).hexdigest(),
                'physical_ledger': str(ledger.path), 'physical_capture_root': str(capture_root),
                'active_physical_caps': ledger.caps,
                'recovery_required': recovery_required,
                'mode': 'restricted_recovery' if recovery_required else 'runtime_batch' if batch else 'delivery',
                'budget_batch': None if batch is None else {key: batch[key] for key in
                    ('id', 'manifest_path', 'manifest_sha256', 'semantic_predecessor')},
                'all_remote_requests_serialized': True, 'new_requests_at_startup': 0,
                'provider_admission': [{'provider_id': provider.provider_id,
                    'scope_hash': provider.admission.scope_hash,
                    'control_directory': str(provider.admission.directory),
                    'automatic_probe': False} for provider in providers],
                'semantic_page_runtime_configured': semantic_configured}
    target = profile.root / 'runtime.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
    return app


def check_delivery_configuration(config_path=None, providers_path=None, run_root=None, *, budget_batch=None):

    profile = load_profile(config_path, providers_path, run_root)
    batch = load_budget_batch(profile, budget_batch or os.getenv('KB_DELIVERY_BUDGET_BATCH') or None)
    state = profile.public()
    path = Path(batch['ledger_path']) if batch else profile.root / 'physical-calls.sqlite3'
    state['budget_batch'] = None if batch is None else batch['id']
    semantic_config = os.getenv('KB_SEMANTIC_PAGE_CONFIG')
    state['semantic_page_runtime'] = validate_semantic_runtime(semantic_config) if semantic_config else None
    snapshot = snapshot_budget(path) if path.exists() else None
    if snapshot and snapshot['summary']['over_budget']:
        raise ValueError('PREDECESSOR_ALREADY_OVER_BUDGET')
    if snapshot and snapshot['caps'] != (batch['caps'] if batch else profile.config['physical_call_caps']):
        raise DeliveryConfigurationError('DELIVERY_PHYSICAL_CAPS_CHANGED')
    state['physical_ledger'] = snapshot['summary'] if snapshot else None
    state['recovery_required'] = bool(state['physical_ledger'] and state['physical_ledger']['pending_or_unknown'])
    state['mode'] = 'restricted_recovery' if state['recovery_required'] else 'runtime_batch' if batch else 'delivery'
    state['recovery_reason'] = 'UNRESOLVED_PHYSICAL_REQUEST' if state['recovery_required'] else None
    return state

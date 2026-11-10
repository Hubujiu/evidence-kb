from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from .application_storage import StorageClient, storage_methods
from .foundation_services import SCHEMAS as FOUNDATION_SCHEMAS, foundation_mounts
from .authentication import StaticAuthentication
from .knowledge_capabilities import build_knowledge_application
from .knowledge_contracts import ModelClient, domain_call
from .knowledge_authority_contract import authority_database
from .knowledge_model_ports import (EmbeddingClient, METHODS as MODEL_METHODS,
                                    OCRClient, TextClient)
from .plugin_configuration import CONTEXT_PROPERTIES, POSITIVE, TEXT, config_schema
from .session_identity import Actor, PlatformError


DEFAULT_APPLICATION = Path(__file__).resolve().parents[1] / 'config/applications/knowledge.json'


def load_knowledge_application(profile=None, *, settings, environment):
    selected = deepcopy(profile) if isinstance(profile, dict) else json.loads(
        Path(profile or DEFAULT_APPLICATION).read_text('utf-8'))
    if profile is None:
        configs = {name: row.setdefault('config', {}) for name, row in selected['plugins'].items()}
        storage_path = environment.get('KB_STORAGE_CONFIG')
        if storage_path is None and settings.storage_file is not None and (
                settings.storage_file.resolve() != Path('.local/storage.json').resolve()):
            storage_path = settings.storage_file
        if storage_path is not None:
            configs['storage']['path'] = str(storage_path)
        configs['access']['identities_file'] = str(settings.identities_file)
        for key, name in (('config_path', 'KB_MODELS_CONFIG'), ('providers_config', 'KB_PROVIDERS_CONFIG')):
            if environment.get(name):
                configs['llm'][key] = environment[name]
        if environment.get('KB_MODELS_CONFIG'):
            configs['llm'].pop('providers_config', None)
            configs['llm'].setdefault('budget_binding', 'provider.primary')
        if not set(configs['llm']) & {'config_path', 'providers_config'}:
            configs['llm']['providers_config'] = '.local/providers.json'
        if environment.get('KB_RESEARCH_ROOT'):
            configs['data']['research_root'] = environment['KB_RESEARCH_ROOT']
        if environment.get('KB_SEMANTIC_PAGE_CONFIG'):
            configs['data']['semantic_config'] = environment['KB_SEMANTIC_PAGE_CONFIG']
        for key, name in (('input_budget', 'KB_AGENT_INPUT_BUDGET'),
                          ('output_reserve', 'KB_AGENT_OUTPUT_RESERVE'),
                          ('model_window', 'KB_AGENT_MODEL_WINDOW'),
                          ('safety_reserve', 'KB_AGENT_SAFETY_RESERVE')):
            if environment.get(name):
                configs['context'][key] = int(environment[name])
        configs['model']['policy'] = {key: value for key, value in configs['context'].items()
                                     if key in CONTEXT_PROPERTIES}
        for key, name in (('max_calls', 'KB_AGENT_MAX_CALLS'), ('max_tokens', 'KB_AGENT_MAX_TOKENS')):
            if environment.get(name):
                configs['model'].setdefault('budget', {})[key] = int(environment[name])
    return selected


def application_schemas():
    from .knowledge_profile import knowledge_plugins

    retrieval = next(spec.config_schema['properties']['retrieval_config']
                     for spec in knowledge_plugins(None, None) if spec.key == 'knowledge.service')
    schemas = {
        'authority': config_schema({'binding': TEXT}, required=('binding',)),
        'model_policy': config_schema(),
        'sessions': config_schema(),
        'data': config_schema({'research_root': TEXT, 'rerank': {'type': 'boolean'}, 'semantic_config': TEXT}),
        'model': config_schema({'routes': config_schema({'generation': TEXT, 'embedding': TEXT, 'ocr': TEXT}),
            'policy': config_schema(CONTEXT_PROPERTIES),
            'budget': config_schema({'max_calls': POSITIVE, 'max_tokens': POSITIVE})}),
        'context': config_schema({**CONTEXT_PROPERTIES,
            'evidence_strategy': {'enum': ['prefix', 'query_blocks', 'coverage_blocks']}}),
        'workflow': config_schema({'retrieval_config': retrieval,
            'max_tool_attempts': {'type': 'integer', 'minimum': 1, 'maximum': 3}},
            required=('retrieval_config',)),
        'access': config_schema({'identities_file': TEXT}, required=('identities_file',)),
        'entry': config_schema(),
    }
    return {**FOUNDATION_SCHEMAS, **{('knowledge.' + key, '2' if key in {'sessions', 'authority'} else '1'): value
                                   for key, value in schemas.items()}}


def create_contract_knowledge(settings, *, profile=None, environment=None, storage_configuration=None,
                              provider_catalog=None, models=None, model_clients=None,
                              platform=None, research=None, workflow_options=None,
                              extra_catalog=(), extra_mounts=None, schemas=None, process_root=None,
                              semantic_observer=None):
    from .application_composition import ApplicationComposition
    from .knowledge_platform_contract import PlatformClient

    selected = load_knowledge_application(profile, settings=settings, environment=environment or {})
    if profile is None and models is not None and not any((environment or {}).get(name)
            for name in ('KB_MODELS_CONFIG', 'KB_PROVIDERS_CONFIG')):
        selected['plugins']['llm']['config'].pop('providers_config', None)
    configured_schemas = application_schemas()
    configured_schemas.update(schemas or {})
    from .agent_context import ContextPolicy
    from .context_budget import ContextBudget
    from .retrieval_pipeline import PipelineConfig

    for row in selected['plugins'].values():
        config = row.get('config', {})
        if row['implementation'] == 'knowledge.context':
            ContextPolicy(**config)
        elif row['implementation'] == 'knowledge.model':
            ContextBudget(**config.get('policy', {}))
        elif row['implementation'] == 'knowledge.workflow':
            PipelineConfig.parse(config['retrieval_config'])

    def session_factory(ctx, config):
        from .knowledge_sessions import KnowledgeSessions
        from .session_store import SessionStore
        from .knowledge_schema import knowledge_schema

        store = SessionStore(database=authority_database(ctx.require('authority')), schema=knowledge_schema)
        ctx.defer(store.close)
        return KnowledgeSessions(store)

    def model_factory(ctx, config):
        from .knowledge_model_service import knowledge_model

        return knowledge_model(ctx.require('llm'), config, ctx.require('access'))

    def data_factory(ctx, config):
        from .agent_tools import EvidenceGateway
        from .core import Platform
        from .document_parser import parse
        from .embedding_jobs import EmbeddingJobs
        from .knowledge_storage import KnowledgeRepository
        from .research import ResearchService
        from .retrieval_pipeline import LunaCandidateReranker
        from .search_evidence import DocumentEvidenceSource
        from .search_router import SplitSearch
        from .storage import OriginalStore

        model = ModelClient(ctx.require('model'))
        text = TextClient(model)
        auxiliary = ctx.require('models')
        metadata = auxiliary.metadata({})
        embedding = EmbeddingClient(auxiliary, metadata['embedding']) if metadata['embedding'] else None
        ocr = OCRClient(auxiliary, metadata['ocr']) if metadata['ocr'] else None
        selected_platform = platform
        if selected_platform is None:
            storage = ctx.require('storage')
            repository = KnowledgeRepository(database=authority_database(ctx.require('authority')))
            source = DocumentEvidenceSource(StorageClient(storage, 'search.source'))
            search = SplitSearch(StorageClient(storage, 'search.bm25'), StorageClient(storage, 'search.dense'), source)
            selected_platform = Platform(repository=repository, search=search,
                store=OriginalStore(bound=StorageClient(storage, 'originals.objects')), parser=parse,
                embedding_jobs=EmbeddingJobs(repository), embedding_provider=embedding, ocr_client=ocr)
        selected_research = research or ResearchService(selected_platform, text, embedding,
            root=config.get('research_root', '.local/research-v4'))
        gateway = EvidenceGateway(selected_platform, selected_research, provider_id=model.provider_id,
            embedding_provider=embedding,
            pipeline_reranker=LunaCandidateReranker(text) if config.get('rerank', True) else None)
        gateway.semantic_pages = None
        if config.get('semantic_config'):
            from .semantic_page_runtime import from_config

            gateway.semantic_pages = from_config(selected_platform, gateway, config['semantic_config'])
            ctx.defer(gateway.semantic_pages.provider.close)
            if semantic_observer is not None:
                semantic_observer(gateway.semantic_pages)
        return gateway

    def authenticate(token, config):
        values = json.loads(Path(config['identities_file']).read_text('utf-8'))
        if not isinstance(values, dict) or not values:
            raise ValueError('INVALID_IDENTITY_CONFIGURATION')
        selected = StaticAuthentication({key: Actor(**actor) for key, actor in values.items()})
        actor = selected.authenticate(token)
        if actor is not None:
            return actor
        raise PlatformError('AUTHENTICATION_FAILED', 'Identity verification failed', 401)

    def access_factory(ctx, config):
        from .knowledge_access import KnowledgeAccess
        from .knowledge_storage import KnowledgeRepository

        repository = KnowledgeRepository(database=authority_database(ctx.require('authority')))
        return KnowledgeAccess(repository)

    from .knowledge_storage_configuration import REQUIREMENTS, SCHEMAS

    foundation = foundation_mounts(storage_configuration=storage_configuration, provider_catalog=provider_catalog,
        models=models, model_clients=model_clients, storage_requirements=REQUIREMENTS, storage_schemas=SCHEMAS)
    composed, application = build_knowledge_application(selected,
        foundation=foundation, session_factory=session_factory, data_factory=data_factory,
        model_factory=model_factory, authenticate=authenticate,
        access_factory=access_factory,
        registry_extensions={('storage.service', 1): storage_methods(),
                             ('knowledge.model.capabilities', 1): MODEL_METHODS},
        schemas=configured_schemas, workflow_options=workflow_options,
        extra_catalog=extra_catalog, extra_mounts=extra_mounts, process_root=process_root)
    try:
        class PlatformDependency:
            def __getattr__(self, name):
                return lambda request: composed.run({'operation': 'platform',
                    'request': {'method': name, 'request': request}})
        service = PlatformClient(PlatformDependency())
        service.close = composed.close
        return ApplicationComposition(composed.host, service, SimpleNamespace(configured=True), application,
            SimpleNamespace(configured=True),
            lambda token: Actor(**domain_call(lambda request: composed.run(
                {'operation': 'authenticate', 'request': request}), {'token': token})))
    except BaseException:
        composed.close()
        raise

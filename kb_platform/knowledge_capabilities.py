from dataclasses import asdict

from .agent_context import ContextManager, ContextPolicy
from .provider_runtime import ToolExecutor
from .agent_engine import Runtime
from .foundation_services import CATALOG as FOUNDATION_CATALOG, registry_extensions as foundation_contracts
from .knowledge_application import KnowledgeApplication, KnowledgeGraphDriver, KnowledgeTools
from .knowledge_context import KnowledgeContext, LocalEvidenceSource, LocalHistorySource, PREPARE_REQUEST, SOURCE_REQUEST
from .knowledge_contracts import (
    ACTOR, APPLICATION_OPERATIONS, CONTEXT_RESPONSE, EMPTY, EVIDENCE_METHODS, MODEL_METHODS, OBJECT,
    POLICY_RESPONSE,
    SESSION_OPERATIONS, TEXT, ApplicationClient, ApplicationEndpoint, EvidenceClient,
    EvidenceEndpoint, ModelClient, ModelEndpoint, SessionClient, SessionEndpoint,
    domain_call, methods, object_schema,
)
from .knowledge_service import KnowledgeService
from .knowledge_workflow import KnowledgeWorkflow
from .plugin_contracts import ContractApplication, ContractRegistry, Method


def contract(name):
    return {'id': name, 'major': 2 if name in {'knowledge.sessions', 'knowledge.authority.storage'} else 1}


def requirement(name):
    return {'contract': contract(name)}


def definition(name, provides, requires):
    return {'implementation': 'knowledge.' + name, 'version': '2' if name in {'sessions', 'authority'} else '1',
            'executor': 'local',
            'provides': {key: contract(value) for key, value in provides.items()},
            'requires': {key: requirement(value) for key, value in requires.items()}}


CATALOG = [
    *FOUNDATION_CATALOG,
    definition('authority', {'authority': 'knowledge.authority.storage'}, {'storage': 'storage.service'}),
    definition('model_policy', {'policy': 'llm.policy'}, {'access': 'knowledge.access'}),
    definition('model', {'model': 'knowledge.model', 'capabilities': 'knowledge.model.capabilities'},
               {'llm': 'llm.service', 'access': 'knowledge.access'}),
    definition('sessions', {'sessions': 'knowledge.sessions'}, {'authority': 'knowledge.authority.storage'}),
    definition('data', {'evidence': 'knowledge.evidence', 'platform': 'knowledge.platform',
                        'semantic': 'knowledge.semantic'},
               {'storage': 'storage.service', 'authority': 'knowledge.authority.storage', 'model': 'knowledge.model',
                'models': 'knowledge.model.capabilities'}),
    definition('access', {'access': 'knowledge.access'}, {'authority': 'knowledge.authority.storage'}),
    definition('context', {'context': 'knowledge.context'},
               {'sessions': 'knowledge.sessions', 'data': 'knowledge.evidence', 'access': 'knowledge.access'}),
    definition('workflow', {'workflow': 'knowledge.workflow'},
               {'sessions': 'knowledge.sessions', 'data': 'knowledge.evidence',
                'model': 'knowledge.model', 'context': 'knowledge.context', 'access': 'knowledge.access',
                'semantic': 'knowledge.semantic'}),
    definition('entry', {'application': 'application.run'},
               {'workflow': 'knowledge.workflow', 'access': 'knowledge.access', 'platform': 'knowledge.platform'}),
]


class DomainDependency:
    def __init__(self, dependency):
        self._dependency = dependency

    def __getattr__(self, name):
        method = getattr(self._dependency, name)
        return lambda request: domain_call(method, request)


class AuthorizedEvidenceClient(EvidenceClient):
    def __init__(self, dependency, access, model=None):
        super().__init__(dependency, model)
        self._access = access

    def authorize(self, actor, scope_id):
        return domain_call(self._access.authorize, {'actor': asdict(actor), 'scope_id': scope_id})


def registry(extensions):
    from .knowledge_authority_contract import METHODS as AUTHORITY_METHODS
    from .knowledge_platform_contract import METHODS as PLATFORM_METHODS

    alternatives = [object_schema({'operation': {'const': name}, 'request': operation.method().request})
                    for name, operation in APPLICATION_OPERATIONS.items()]
    alternatives.extend([
        object_schema({'operation': {'const': 'authenticate'},
                       'request': object_schema({'token': TEXT})}),
        object_schema({'operation': {'const': 'platform'}, 'request': {'oneOf': [
            object_schema({'method': {'const': name}, 'request': method.request})
            for name, method in PLATFORM_METHODS.items()]}}),
    ])
    return ContractRegistry({
        ('application.run', 1): {'run': Method({'oneOf': alternatives}, {})},
        ('knowledge.workflow', 1): methods(APPLICATION_OPERATIONS),
        ('knowledge.sessions', 2): methods(SESSION_OPERATIONS),
        ('knowledge.evidence', 1): EVIDENCE_METHODS,
        ('knowledge.model', 1): MODEL_METHODS,
        ('knowledge.authority.storage', 2): AUTHORITY_METHODS,
        ('knowledge.context', 1): {
            'prepare_request': Method(PREPARE_REQUEST, CONTEXT_RESPONSE),
            'sources': Method(SOURCE_REQUEST, {'type': 'array'}),
            'metadata': Method(EMPTY, object_schema({'policy': POLICY_RESPONSE}))},
        ('knowledge.access', 1): {
            'authenticate': Method(object_schema({'token': TEXT}), ACTOR),
            'authorize': EVIDENCE_METHODS['authorize'],
            'validate_embedding': Method(object_schema({'audit_context': OBJECT,
                'task': {'enum': ['document', 'query']}, 'provider_id': TEXT}),
                object_schema({'authorized': {'type': 'boolean'}}))},
        **foundation_contracts(), **extensions,
    })


def build_knowledge_application(application, *, foundation, session_factory, data_factory,
                                model_factory, authenticate, registry_extensions, access_factory=None, schemas=None,
                                workflow_options=None, extra_catalog=(), extra_mounts=None,
                                process_root=None):
    from .knowledge_platform_contract import METHODS as PLATFORM_METHODS, PlatformEndpoint
    from .knowledge_semantic_contract import METHODS as SEMANTIC_METHODS, SemanticClient, SemanticEndpoint
    from .knowledge_authority_contract import AuthorityStorageEndpoint, validate_authority_bindings
    from .knowledge_model_service import KnowledgeModelPolicy

    validate_authority_bindings(application)

    def authority(ctx, config):
        ctx.provide('authority', AuthorityStorageEndpoint(ctx.require('storage'), binding=config['binding']))

    def model_policy(ctx, config):
        ctx.provide('policy', KnowledgeModelPolicy(ctx.require('access')))

    def model(ctx, config):
        selected = model_factory(ctx, config)
        endpoint = ModelEndpoint(selected)
        ctx.defer(endpoint.close)
        ctx.defer(selected.capability_endpoint.close)
        ctx.provide('model', endpoint)
        ctx.provide('capabilities', selected.capability_endpoint)

    def sessions(ctx, config):
        ctx.provide('sessions', SessionEndpoint(session_factory(ctx, config)))

    def data(ctx, config):
        gateway = data_factory(ctx, config)
        evidence = EvidenceEndpoint(gateway)
        platform = PlatformEndpoint(gateway.platform)
        ctx.defer(evidence.close)
        ctx.defer(platform.close)
        ctx.provide('evidence', evidence)
        ctx.provide('platform', platform)
        ctx.provide('semantic', SemanticEndpoint(getattr(gateway, 'semantic_pages', None)))

    def access(ctx, config):
        if access_factory is None:
            raise ValueError('KNOWLEDGE_ACCESS_FACTORY_REQUIRED')
        authority = access_factory(ctx, config)

        class Access:
            def authenticate(self, request):
                actor = authenticate(request['token'], config)
                return asdict(actor) if not isinstance(actor, dict) else actor

            def authorize(self, request):
                return authority.authorize(request)

            def validate_embedding(self, request):
                return authority.validate_embedding(request)
        ctx.provide('access', Access())

    def context(ctx, config):
        policy = ContextPolicy(**config)
        service = KnowledgeContext(LocalHistorySource(SessionClient(ctx.require('sessions'))),
            LocalEvidenceSource(AuthorizedEvidenceClient(ctx.require('data'), ctx.require('access'))),
            assembler=ContextManager(policy))

        class Context:
            def prepare_request(self, request):
                return service.prepare_request(request)

            def sources(self, request):
                return service.sources(request)

            def metadata(self, request):
                return {'policy': asdict(policy)}
        ctx.provide('context', Context())

    def workflow(ctx, config):
        model = ModelClient(ctx.require('model'))
        context = DomainDependency(ctx.require('context'))
        policy = ContextPolicy(**context.metadata({})['policy'])
        semantic = ctx.require('semantic')
        semantic_pages = SemanticClient(semantic) if semantic.metadata({})['available'] else None
        knowledge = KnowledgeService(None, model, store=SessionClient(ctx.require('sessions')),
            gateway=AuthorizedEvidenceClient(ctx.require('data'), ctx.require('access'), model),
            context_policy=policy, semantic_pages=semantic_pages, context_manager=None, context_service=context,
            retrieval_config=config['retrieval_config'], tool_executor=ToolExecutor(
                max_attempts=config.get('max_tool_attempts', 2)), runtime=Runtime(),
            workflow_factory=KnowledgeWorkflow, **(workflow_options or {}))
        application = KnowledgeApplication(knowledge, KnowledgeTools(knowledge),
                                           KnowledgeGraphDriver(ctx._context.host))
        ctx.provide('workflow', ApplicationEndpoint(application, model))

    def entry(ctx, config):
        workflow = ctx.require('workflow')
        platform = ctx.require('platform')
        access = ctx.require('access')

        class Entry:
            def run(self, request):
                if request['operation'] == 'authenticate':
                    return access.authenticate(request['request'])
                if request['operation'] == 'platform':
                    from .knowledge_platform_contract import METHODS

                    call = request['request']
                    if set(call) != {'method', 'request'} or call['method'] not in METHODS:
                        raise ValueError('UNKNOWN_PLATFORM_OPERATION')
                    return getattr(platform, call['method'])(call['request'])
                return getattr(workflow, request['operation'])(request['request'])
        ctx.provide('application', Entry())

    mounts = {('knowledge.' + name, '2' if name in {'sessions', 'authority'} else '1'): factory for name, factory in {
        'authority': authority, 'model_policy': model_policy, 'model': model, 'sessions': sessions, 'data': data,
        'access': access, 'context': context, 'workflow': workflow, 'entry': entry}.items()}
    mounts.update(foundation)
    mounts.update(extra_mounts or {})
    selected_schemas = {(item['implementation'], item['version']): OBJECT for item in CATALOG}
    selected_schemas.update(schemas or {})
    selected_registry = registry({('knowledge.platform', 1): PLATFORM_METHODS,
                                  ('knowledge.semantic', 1): SEMANTIC_METHODS, **registry_extensions})
    composed = ContractApplication(application, [*CATALOG, *extra_catalog], registry=selected_registry,
        mounts=mounts, schemas=selected_schemas, process_root=process_root)
    return composed, ApplicationClient(composed)

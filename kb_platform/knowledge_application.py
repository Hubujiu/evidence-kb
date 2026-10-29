"""Knowledge application and model-facing capabilities assembled by a profile."""
from copy import deepcopy

from .agent_plugins import ToolContext, ToolPlugin, ToolResult


class KnowledgeTools:
    """Bind trusted session controls outside model-visible tool arguments."""

    def __init__(self, knowledge):
        self.knowledge = knowledge

    def bind_turn(self, actor, session_id, *, request_id, expected_revision,
                  policy=None, constraints=None, workflow_state=None):
        controls = deepcopy(dict(request_id=request_id, expected_revision=expected_revision,
                                 policy=policy, constraints=constraints, workflow_state=workflow_state))

        def query(context, arguments):
            result = self.knowledge.turn(actor, session_id, message=arguments['question'], **controls)
            return ToolResult(result)

        tool = ToolPlugin(
            'kb_query', 'Answer using the authorized knowledge session and verified evidence.',
            {'type': 'object', 'properties': {'question': {'type': 'string', 'minLength': 1,
                                                          'maxLength': 20000}},
             'required': ['question'], 'additionalProperties': False},
            query, mutates_data=True, incurs_cost=True, direct_return=True,
        )
        return tool, ToolContext(actor, session_id, request_id, call_id=request_id)


class KnowledgeApplication:
    """Application policy is a replaceable consumer of knowledge and loop services."""

    def __init__(self, knowledge, tools, loop):
        self.knowledge, self.tools, self.loop = knowledge, tools, loop

    def capabilities(self):
        return self.knowledge.capabilities()

    def create(self, actor, scope_id, policy=None, config_name='recommended'):
        return self.knowledge.create(actor, scope_id, policy, config_name)

    def get(self, actor, session_id):
        return self.knowledge.get(actor, session_id)

    def history(self, actor, session_id):
        return self.knowledge.history(actor, session_id)

    def artifact(self, actor, session_id, artifact_id):
        return self.knowledge.artifact(actor, session_id, artifact_id)

    def sources(self, actor, scope_id):
        return self.knowledge.gateway.sources(actor, scope_id)

    def authority(self, actor, scope_id):
        return self.knowledge.gateway.authorize(actor, scope_id)

    def list_sessions(self, actor, *, limit=50, offset=0):
        from .core import PlatformError

        visible = []
        for row in self.knowledge.store.list_sessions(actor, limit=limit, offset=offset):
            try:
                self.knowledge.gateway.authorize(actor, row['scope_id'])
                visible.append(row)
            except PlatformError as exc:
                if exc.status_code not in (403, 404):
                    raise
        return {'sessions': visible, 'limit': limit, 'offset': offset,
                'next_offset': offset + limit, 'source_text_included': False}

    def turn(self, actor, session_id, *, message, request_id, expected_revision,
             policy=None, constraints=None, workflow_state=None):
        tool, context = self.tools.bind_turn(
            actor, session_id, request_id=request_id, expected_revision=expected_revision,
            policy=policy, constraints=constraints, workflow_state=workflow_state)
        return self.loop.invoke(tool, context, {'question': message})


class HostedKnowledgeApplication:
    """HTTP calls hold a host lease; delivery wiring uses explicit provider properties."""

    def __init__(self, host, application):
        self.host, self._application = host, application

    @property
    def model(self):
        return self._application.knowledge.model

    @property
    def semantic_pages(self):
        return self._application.knowledge.semantic_pages

    @property
    def tool_context(self):
        return self._application.knowledge.tool_context

    @tool_context.setter
    def tool_context(self, value):
        self._application.knowledge.tool_context = value

    def capabilities(self):
        return self.host.call('application', 'capabilities')

    def create(self, actor, scope_id, policy=None, config_name='recommended'):
        return self.host.call('application', 'create', actor, scope_id, policy, config_name)

    def get(self, actor, session_id):
        return self.host.call('application', 'get', actor, session_id)

    def history(self, actor, session_id):
        return self.host.call('application', 'history', actor, session_id)

    def artifact(self, actor, session_id, artifact_id):
        return self.host.call('application', 'artifact', actor, session_id, artifact_id)

    def sources(self, actor, scope_id):
        return self.host.call('application', 'sources', actor, scope_id)

    def authority(self, actor, scope_id):
        return self.host.call('application', 'authority', actor, scope_id)

    def list_sessions(self, actor, *, limit=50, offset=0):
        return self.host.call('application', 'list_sessions', actor, limit=limit, offset=offset)

    def turn(self, actor, session_id, **request):
        return self.host.call('application', 'turn', actor, session_id, **request)

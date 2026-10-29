"""Generic Agent application; concrete drivers and persistence are injected."""
from .harness_sessions import SessionToolContext


class AgentApplication:
    def __init__(self, sessions, loop):
        self.sessions, self.loop = sessions, loop

    def create(self, actor, scope_id='default'):
        return self.sessions.create(actor, scope_id)

    def get(self, actor, session_id):
        return self.sessions.load(actor, session_id)

    def history(self, actor, session_id):
        return self.sessions.history(actor, session_id)

    def turn(self, actor, session_id, *, message, request_id, expected_revision, **limits):
        if not isinstance(message, str) or not message.strip() or len(message) > 20000:
            raise ValueError('INVALID_AGENT_MESSAGE')
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise ValueError('INVALID_AGENT_REQUEST_ID')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('INVALID_AGENT_REVISION')
        context = SessionToolContext(actor, session_id, request_id, expected_revision=expected_revision)
        return self.loop.run(message, context, **limits)


class HostedAgentApplication:
    """Every public operation holds the same host lease as other applications."""

    def __init__(self, host, application):
        self.host, self._application = host, application

    def create(self, actor, scope_id='default'):
        return self.host.call('application', 'create', actor, scope_id)

    def get(self, actor, session_id):
        return self.host.call('application', 'get', actor, session_id)

    def history(self, actor, session_id):
        return self.host.call('application', 'history', actor, session_id)

    def turn(self, actor, session_id, **request):
        return self.host.call('application', 'turn', actor, session_id, **request)

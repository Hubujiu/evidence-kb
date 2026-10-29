"""Knowledge state initialization over the shared durable session backend."""
# Preserve legacy public helpers and clock patching for existing consumers.
from .session_store import SessionStore, canonical as canonical, digest as digest, now as now, time as time
from .knowledge_state import initial_state, validate_failure


class AgentStore(SessionStore):
    """Compatibility adapter; transactions and history are domain-neutral."""

    def __init__(self, root='.local/agentic-v5'):
        super().__init__(root)

    def create(self, actor, scope_id, source_policy, config_name='recommended'):
        return super().create(actor, scope_id, initial_state(source_policy, config_name))

    def fail(self, actor, session_id, token, state, response):
        validate_failure(response)
        return super().fail(actor, session_id, token, state, response)

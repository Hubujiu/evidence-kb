from .knowledge_state import initial_state, validate_failure


class KnowledgeSessions:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    def create(self, actor, scope_id, source_policy, config_name='recommended'):
        return self.store.create(actor, scope_id, initial_state(source_policy, config_name))

    def fail(self, actor, session_id, token, state, response):
        validate_failure(response)
        return self.store.fail(actor, session_id, token, state, response)

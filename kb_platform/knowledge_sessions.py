from .knowledge_state import initial_state, validate_failure
from .knowledge_access import locked_authority
from .knowledge_storage import KnowledgeTransaction
from .session_identity import PlatformError


class KnowledgeSessions:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    def create(self, actor, scope_id, source_policy, config_name='recommended'):
        return self.store.create(actor, scope_id, initial_state(source_policy, config_name))

    def finish(self, actor, session_id, token, state, response, *, authority, assistant_event=None):
        if (type(authority) is not dict or set(authority) != {'scope_id', 'manifest_id', 'authority_revision'}
                or type(authority['scope_id']) is not str or not authority['scope_id']
                or authority['manifest_id'] is not None and type(authority['manifest_id']) is not str
                or type(authority['authority_revision']) is not int or authority['authority_revision'] < 0):
            raise ValueError('INVALID_COMMIT_AUTHORITY')
        scope = authority['scope_id']
        with self.store.database.transaction(scope={'tenant_id': actor.tenant_id, 'kb_id': scope}) as transaction:
            current = locked_authority(KnowledgeTransaction(transaction, actor, scope, True), actor, scope)
            if current != authority:
                raise PlatformError('SCOPE_CHANGED', 'Authority changed before answer commit', 409)
            return self.store.finish_in_transaction(transaction, actor, session_id, token, state, response,
                expected_scope=scope, assistant_event=assistant_event)

    def fail(self, actor, session_id, token, state, response):
        validate_failure(response)
        return self.store.fail(actor, session_id, token, state, response)

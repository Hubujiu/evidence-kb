from .knowledge_schema import knowledge_schema
from .persistence_schema import budget_schema
from .session_storage import session_schema
from .storage_sdk import DOCUMENT_V1, LEXICAL_BM25_V1, OBJECT_V1, VECTOR_V1
from .storage_sdk.relational import GUARDED_RELATIONAL_V1


REQUIREMENTS = {
    'repository': {'primary': GUARDED_RELATIONAL_V1},
    'store': {'primary': GUARDED_RELATIONAL_V1},
    'provider': {'primary': GUARDED_RELATIONAL_V1},
    'search': {'bm25': LEXICAL_BM25_V1, 'dense': VECTOR_V1, 'source': DOCUMENT_V1},
    'originals': {'objects': OBJECT_V1},
}
SCHEMAS = {'repository.primary': knowledge_schema, 'store.primary': session_schema,
           'provider.primary': budget_schema}

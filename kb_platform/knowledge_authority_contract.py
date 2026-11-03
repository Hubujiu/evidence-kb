from .database_contracts import CommitUnknown, DatabaseError
from .knowledge_contracts import EMPTY, OBJECT, TEXT, object_schema
from .knowledge_schema import knowledge_schema
from .plugin_contracts import ContractError, Method
from .plugin_native import PluginError
from .storage_expression_wire import schema_descriptor, schema_semantics
from .storage_sdk import SpaceKey, StorageIdentity
from .storage_structured_wire import PortableStructuredClient


PROTOCOL = 'knowledge.authority.storage/2'
METHODS = {
    'describe': Method(EMPTY, object_schema({'identity': OBJECT,
        'methods': {'type': 'array', 'items': TEXT}, 'schema': OBJECT})),
    'relational': Method(OBJECT, OBJECT),
}


def authority_schema_descriptor(owner=None):
    descriptor = schema_descriptor(knowledge_schema(owner or 'knowledge-authority'), include_digest=owner is not None)
    for table in descriptor['tables']:
        table['constraints'].sort(key=lambda item: str(item))
    if owner is None:
        descriptor.pop('owner')
    return descriptor


class AuthorityStorageEndpoint:
    def __init__(self, storage, *, binding='repository.primary'):
        self._storage, self._binding = storage, binding

    def describe(self, request):
        selected = self._storage.describe({'binding': self._binding})
        owner = selected['identity']['space']['owner_id']
        return {'identity': selected['identity'], 'methods': ['describe', 'relational'],
                'schema': authority_schema_descriptor(owner)}

    def relational(self, request):
        return self._storage.relational({'binding': self._binding, 'request': request})


def authority_database(dependency):
    description = dependency.describe({})
    try:
        identity = description['identity']
        selected = StorageIdentity(SpaceKey(**identity['space']), identity['dataset_id'])
        expected = {'owner': selected.space.owner_id, **authority_schema_descriptor()}
        if (type(identity['dataset_id']) is not str or not identity['dataset_id'] or len(identity['dataset_id']) > 200
                or type(description['methods']) is not list or 'relational' not in description['methods']
                or schema_semantics(description['schema']) != schema_semantics(expected)):
            raise DatabaseError('KNOWLEDGE_AUTHORITY_SCHEMA_MISMATCH')
    except (KeyError, TypeError, ValueError, DatabaseError):
        raise DatabaseError('KNOWLEDGE_AUTHORITY_SCHEMA_MISMATCH') from None

    def invoke(request):
        try:
            return dependency.relational(request)
        except ContractError as error:
            if error.code == 'COMMIT_UNKNOWN':
                raise CommitUnknown(error.details['operation_id']) from error
            raise DatabaseError(error.code) from error

    return PortableStructuredClient(invoke, identity=selected)


def validate_authority_bindings(application):
    consumers = [name for name, definition in application.get('plugins', {}).items()
                 if definition.get('implementation') in {'knowledge.access', 'knowledge.data', 'knowledge.sessions'}]
    kinds = {application['plugins'][name]['implementation'] for name in consumers}
    if not {'knowledge.access', 'knowledge.data'} <= kinds:
        return
    bindings = [application.get('bindings', {}).get(name, {}).get('authority') for name in consumers]
    if any(not isinstance(binding, dict) or set(binding) != {'plugin', 'export'} for binding in bindings):
        raise PluginError('KNOWLEDGE_AUTHORITY_BINDING_REQUIRED')
    if any(binding != bindings[0] for binding in bindings[1:]):
        raise PluginError('KNOWLEDGE_AUTHORITY_BINDING_MISMATCH')

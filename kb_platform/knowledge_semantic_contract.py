from dataclasses import asdict

from .knowledge_contracts import ACTOR, ARRAY, EMPTY, INTEGER, OBJECT, TEXT, domain_call, object_schema
from .plugin_contracts import ContractError, Method
from .session_identity import Actor, PlatformError


SOURCE = {'actor': ACTOR, 'kb_id': TEXT}
METHODS = {
    'metadata': Method(EMPTY, object_schema({'available': {'type': 'boolean'}})),
    'current_manifest': Method(object_schema(SOURCE), object_schema({'manifest': OBJECT, 'checksum': TEXT})),
    'query': Method(object_schema({**SOURCE, 'query': TEXT, 'policy': OBJECT,
        'limit': INTEGER, 'exclude_citation_ids': ARRAY, 'call_context': {'type': ['object', 'null']}},
        ['actor', 'kb_id', 'query', 'policy']), object_schema({'records': ARRAY, 'receipt': OBJECT})),
}


class SemanticEndpoint:
    def __init__(self, service):
        self._service = service

    def metadata(self, request):
        return {'available': self._service is not None}

    def _require(self):
        if self._service is None:
            raise PlatformError('SEMANTIC_PAGE_INDEX_UNAVAILABLE', 'Semantic Page service is not configured', 409)
        return self._service

    def current_manifest(self, request):
        try:
            manifest, checksum = self._require().current_manifest(Actor(**request['actor']), request['kb_id'])
        except PlatformError as error:
            if error.code == 'SEMANTIC_PAGE_INDEX_UNAVAILABLE':
                raise ContractError(error.code, str(error), status_code=error.status_code,
                    details={'missing_index': isinstance(error.__cause__, FileNotFoundError)}) from error
            raise
        return {'manifest': manifest, 'checksum': checksum}

    def query(self, request):
        values = {key: value for key, value in request.items() if key != 'actor'}
        result = self._require().query(Actor(**request['actor']), **values)
        return {'records': list(result), 'receipt': result.receipt}


class SemanticClient:
    def __init__(self, dependency):
        self._dependency = dependency

    def current_manifest(self, actor, kb_id):
        try:
            result = self._dependency.current_manifest({'actor': asdict(actor), 'kb_id': kb_id})
        except ContractError as error:
            if error.code == 'SEMANTIC_PAGE_INDEX_UNAVAILABLE' and error.details.get('missing_index'):
                raise PlatformError(error.code, str(error), error.status_code) from FileNotFoundError()
            raise PlatformError(error.code, str(error), error.status_code) from error
        return result['manifest'], result['checksum']

    def query(self, actor, kb_id, query, policy, limit=5, *, exclude_citation_ids=(), call_context=None):
        from .semantic_pages_v2 import SemanticPageResults

        result = domain_call(self._dependency.query, {'actor': asdict(actor), 'kb_id': kb_id,
            'query': query, 'policy': policy, 'limit': limit,
            'exclude_citation_ids': list(exclude_citation_ids), 'call_context': call_context})
        return SemanticPageResults(result['records'], result['receipt'])

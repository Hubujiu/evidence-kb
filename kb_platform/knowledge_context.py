from copy import deepcopy
import json

from jsonschema import Draft202012Validator

from .planning_references import project_references
from .session_identity import Actor, PlatformError


ACTOR = {'type': 'object', 'properties': {
    'tenant_id': {'type': 'string', 'minLength': 1},
    'principal_id': {'type': 'string', 'minLength': 1}},
    'required': ['tenant_id', 'principal_id'], 'additionalProperties': False}
SOURCE_REQUEST = {'type': 'object', 'properties': {
    'actor': ACTOR, 'scope': {'type': 'string', 'minLength': 1}},
    'required': ['actor', 'scope'], 'additionalProperties': False}
HISTORY_REQUEST = {'type': 'object', 'properties': {
    'actor': ACTOR, 'session_id': {'type': 'string', 'minLength': 1},
    'expected_revision': {'type': 'integer', 'minimum': 0}},
    'required': ['actor', 'session_id', 'expected_revision'], 'additionalProperties': False}
HISTORY_RESPONSE = {'type': 'object', 'properties': {
    'revision': {'type': 'integer', 'minimum': 0}, 'scope': {'type': 'string', 'minLength': 1},
    'events': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'seq': {'type': 'integer', 'minimum': 1}, 'kind': {'type': 'string'},
        'payload': {'type': 'object'}}, 'required': ['seq', 'kind', 'payload']}}},
    'required': ['revision', 'scope', 'events'], 'additionalProperties': False}
EVIDENCE = {'type': 'array', 'items': {'type': 'object', 'properties': {
    'citation_id': {'type': 'string', 'minLength': 1}}, 'required': ['citation_id']}}
PREPARE_PROPERTIES = {
    'actor': ACTOR, 'scope': {'type': 'string', 'minLength': 1},
    'session_id': {'type': 'string', 'minLength': 1},
    'expected_revision': {'type': 'integer', 'minimum': 0},
    'authority': {'type': 'object'}, 'evidence': EVIDENCE,
    'source_policy': {'type': 'object'}, 'evidence_offsets': {'type': 'object',
        'additionalProperties': {'type': 'integer', 'minimum': 0}},
    'source_epoch': {'type': 'integer', 'minimum': 0},
    'history_invalidated': {'type': 'boolean'}, 'reuse': {'type': 'boolean'},
    'before_seq': {'type': 'integer', 'minimum': 1},
    'role': {'type': 'string', 'minLength': 1}, 'system': {'type': 'string'},
    'state': {'type': 'object'}, 'question': {'type': 'string'},
    'checkpoint': {'type': ['object', 'null']},
    'selection_query': {'type': ['string', 'null']},
    'aliases': {'type': 'object', 'additionalProperties': {'type': 'string'}},
    'rerank_payload': {'type': ['object', 'null']},
}
PREPARE_REQUEST = {'type': 'object', 'properties': PREPARE_PROPERTIES,
                   'required': list(PREPARE_PROPERTIES), 'additionalProperties': False}


def _request(value, schema, code='INVALID_CONTEXT_REQUEST', status=422):
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError) as error:
        raise PlatformError(code, 'Context protocol requires JSON values', status) from error
    error = next(Draft202012Validator(schema).iter_errors(copied), None)
    if error is not None:
        raise PlatformError(code, error.message, status)
    return copied


class LocalHistorySource:
    def __init__(self, store):
        self._store = store

    def read(self, request):
        request = _request(request, HISTORY_REQUEST)
        actor = Actor(**request['actor'])
        before = self._store.load(actor, request['session_id'])
        if before['revision'] != request['expected_revision']:
            raise PlatformError('SESSION_REVISION_CONFLICT', 'Session changed before context retrieval', 409)
        events = self._store.events(actor, request['session_id'])
        after = self._store.load(actor, request['session_id'])
        if before['revision'] != after['revision'] or before['scope_id'] != after['scope_id']:
            raise PlatformError('SESSION_REVISION_CONFLICT', 'Session changed during context retrieval', 409)
        return {'revision': before['revision'], 'scope': before['scope_id'], 'events': events}


class LocalEvidenceSource:
    def __init__(self, gateway):
        self._gateway = gateway

    def authorize(self, request):
        return self._gateway.authorize(Actor(**request['actor']), request['scope'])

    def revalidate(self, request):
        return self._gateway.revalidate(Actor(**request['actor']), request['scope'],
                                        request['evidence'], request['source_policy'])

    def sources(self, request):
        return self._gateway.sources(Actor(**request['actor']), request['scope'])


class KnowledgeContext:
    def __init__(self, history_source, evidence_source, *, assembler):
        self._history, self._evidence, self._assembler = history_source, evidence_source, assembler

    def sources(self, request):
        request = _request(request, SOURCE_REQUEST)
        return deepcopy(self._evidence.sources(request))

    def prepare_request(self, request):
        request = _request(request, PREPARE_REQUEST)
        source = {key: request[key] for key in ('actor', 'scope')}
        if self._evidence.authorize(source) != request['authority']:
            raise PlatformError('SCOPE_CHANGED', 'Authorization or manifest changed during turn', 409)
        evidence = _request(self._evidence.revalidate({**source, 'evidence': request['evidence'],
            'source_policy': request['source_policy']}), EVIDENCE, 'CONTEXT_EVIDENCE_SOURCE_INVALID', 409)
        allowed = {item['citation_id'] for item in request['evidence']}
        identities = [item['citation_id'] for item in evidence]
        if not set(identities) <= allowed or len(set(identities)) != len(identities):
            raise PlatformError('CONTEXT_EVIDENCE_SOURCE_INVALID', 'Source returned unrequested or repeated evidence', 409)
        evidence = [{**item, 'context_offset': request['evidence_offsets'].get(item['citation_id'], 0)}
                    for item in evidence]
        history = []
        if not request['history_invalidated'] and request['rerank_payload'] is None:
            snapshot = _request(self._history.read({key: request[key] for key in
                ('actor', 'session_id', 'expected_revision')}), HISTORY_RESPONSE,
                'CONTEXT_HISTORY_SOURCE_INVALID', 409)
            if snapshot['revision'] != request['expected_revision'] or snapshot['scope'] != request['scope']:
                raise PlatformError('SESSION_REVISION_CONFLICT', 'History does not match the requested context', 409)
            history = [event for event in snapshot['events']
                       if event['seq'] < request['before_seq'] and event['kind'] in {'user', 'assistant'}
                       and event['payload'].get('source_epoch') == request['source_epoch']
                       and (event['kind'] == 'user' or (
                           set(event['payload'].get('evidence_ids', [])) <= set(identities)
                           and (request['reuse'] or not event['payload'].get('evidence_ids'))))]
        if self._evidence.authorize(source) != request['authority']:
            raise PlatformError('SCOPE_CHANGED', 'Authorization or manifest changed during context retrieval', 409)
        if request['rerank_payload'] is not None:
            prepared = self._assembler.prepare_rerank(request['system'], evidence, request['rerank_payload'])
        else:
            aliases = request['aliases']
            prepared = self._assembler.prepare(request['role'], request['system'],
                project_references(request['state'], aliases), project_references(history, aliases),
                project_references(evidence, aliases), request['question'], request['checkpoint'],
                selection_query=request['selection_query'])
        return {'prepared': prepared.to_dict(), 'evidence': evidence}

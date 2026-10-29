from hashlib import sha256
import json

from .agent_engine import current_execution
from .agent_plugins import ToolPlugin, ToolResult


def query_tool(service, session_id):

    def query(context, arguments):
        execution = current_execution()
        if execution is None or not context.call_id:
            raise ValueError('KNOWLEDGE_TOOL_REQUIRES_EXECUTION_CONTEXT')
        execution.check()
        session = service.get(context.actor, session_id)

        identity = json.dumps([context.session_id, context.turn_id, context.call_id], ensure_ascii=False,
                              separators=(',', ':'))
        request_id = 'tool-' + sha256(identity.encode('utf-8')).hexdigest()
        result = service.turn(context.actor, session_id, message=arguments['question'],
                              request_id=request_id, expected_revision=session['revision'])
        execution.check()

        return ToolResult({key: value for key, value in result.items() if key != 'trace'})

    return ToolPlugin('kb_query', 'Answer a question using the authorized knowledge session and verified sources.',
        {'type': 'object', 'properties': {'question': {'type': 'string', 'minLength': 1, 'maxLength': 20000}},
         'required': ['question'], 'additionalProperties': False}, query,
        version='1:' + sha256(json.dumps([str(service.store.root), session_id], ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')).hexdigest(),
        mutates_data=True, incurs_cost=True, direct_return=True)

from concurrent.futures import Future
import json
import sys
from threading import Lock, Thread
from uuid import uuid4


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'external.writer': ['compose']}
pending = {}
output = Lock()


def send(value):
    with output:
        print(json.dumps(value), flush=True)


def compose(request):
    identity = uuid4().hex
    future = pending[identity] = Future()
    send({'protocol': PROTOCOL, 'id': identity, 'kind': 'call', 'parent': request['id'],
        'service': 'llm', 'method': 'complete_json',
        'args': [[{'role': 'user', 'content': request['args'][0]}]],
        'kwargs': {'role': 'answer', 'schema': {'type': 'object',
            'properties': {'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False},
            'call_context': {}}})
    response = future.result()
    send({**response, 'id': request['id']})


for line in sys.stdin:
    request = json.loads(line)
    if 'ok' in request:
        pending.pop(request['id']).set_result(request)
    elif request['kind'] == 'handshake':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True,
            'result': {'protocol': PROTOCOL, 'services': SERVICES,
                'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}})
    elif request['kind'] == 'invoke':
        Thread(target=compose, args=(request,), daemon=True).start()
    elif request['kind'] == 'shutdown':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': None})
        break

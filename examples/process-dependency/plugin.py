from concurrent.futures import Future
import json
import sys
from threading import Lock, Thread
from uuid import uuid4


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'external.consumer': ['invoke', 'denied', 'duplicate', 'unknown_parent']}
pending, workers = {}, []
output = Lock()


def send(value):
    with output:
        print(json.dumps(value), flush=True)


def call(request, service, prompt):
    identity = uuid4().hex
    future = pending[identity] = Future()
    frame = {'protocol': PROTOCOL, 'id': identity, 'kind': 'call', 'parent': request['id'],
        'service': service, 'method': 'generate', 'args': [prompt], 'kwargs': {}}
    if request['method'] == 'unknown_parent':
        frame['parent'] = 'unrelated-request'
    send(frame)
    if request['method'] == 'duplicate':
        send(frame)
    return future.result()


def invoke(request):
    service = 'private.model' if request['method'] == 'denied' else 'shared.model'
    first = call(request, service, request['args'][0])
    result = first
    if first['ok']:
        result = call(request, service, first['result']['answer'] + ':continued')
    send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': result})


for line in sys.stdin:
    request = json.loads(line)
    if 'ok' in request:
        pending.pop(request['id']).set_result(request)
    elif request['kind'] == 'handshake':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True,
            'result': {'protocol': PROTOCOL, 'services': SERVICES,
                'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}})
    elif request['kind'] == 'invoke':
        worker = Thread(target=invoke, args=(request,), daemon=True)
        workers.append(worker)
        worker.start()
    elif request['kind'] == 'shutdown':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': None})
        break

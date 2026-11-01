import base64
from concurrent.futures import Future
import json
import sys
from threading import Lock, Thread
from uuid import uuid4


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'external.storage': ['put', 'get', 'denied', 'oversized']}
pending = {}
output, pending_lock = Lock(), Lock()


def send(value):
    with output:
        print(json.dumps(value), flush=True)


def call(request, payload):
    identity, future = uuid4().hex, Future()
    with pending_lock:
        pending[identity] = future
    send({'protocol': PROTOCOL, 'id': identity, 'kind': 'call', 'parent': request['id'],
          'service': 'shared.storage', 'method': 'call_json', 'args': [payload], 'kwargs': {}})
    return future.result()


def invoke(request):
    method = request['method']
    key = {'type': 'json', 'value': request['args'][0]}
    payload = {'method': 'get', 'args': [key]}
    if method == 'put':
        payload = {'method': 'set', 'args': [key, {'type': 'bytes', 'value': base64.b64encode(
            request['args'][1].encode()).decode('ascii')}]}
    elif method == 'denied':
        payload = {'method': '_space', 'args': []}
    elif method == 'oversized':
        payload = {'method': 'get', 'args': [{'type': 'json', 'value': 'x' * (512 * 1024)}]}
    result = call(request, payload)
    send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': result})


for line in sys.stdin:
    request = json.loads(line)
    if 'ok' in request:
        with pending_lock:
            future = pending.pop(request['id'])
        future.set_result(request)
    elif request['kind'] == 'handshake':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True,
              'result': {'protocol': PROTOCOL, 'services': SERVICES,
                         'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}})
    elif request['kind'] == 'invoke':
        Thread(target=invoke, args=(request,), daemon=True).start()
    elif request['kind'] == 'shutdown':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': None})
        break

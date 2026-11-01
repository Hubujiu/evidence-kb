from concurrent.futures import Future
import json
import sys
from threading import Lock, Thread
from uuid import uuid4


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'external.database': ['put', 'get', 'rollback_attempt']}
pending, workers = {}, []
output, pending_lock = Lock(), Lock()


def send(value):
    with output:
        print(json.dumps(value), flush=True)


def call(request, method, payload):
    identity, future = uuid4().hex, Future()
    with pending_lock:
        pending[identity] = future
    send({'protocol': PROTOCOL, 'id': identity, 'kind': 'call', 'parent': request['id'],
          'service': 'shared.database', 'method': method, 'args': [payload], 'kwargs': {}})
    return future.result()


def invoke(request):
    key = request['args'][0]
    if request['method'] == 'get':
        result = call(request, 'query_json', {'statement': {
            'sql': 'SELECT task_id,value FROM tasks WHERE task_id=:task_id', 'params': {'task_id': key}}})
    else:
        statements = [{'sql': 'INSERT INTO tasks(task_id,value) VALUES(:task_id,:value)',
                       'params': {'task_id': key, 'value': request['args'][1]}, 'expected_rows': 1}]
        if request['method'] == 'rollback_attempt':
            statements.append({'sql': '\fCOMMIT'})
        result = call(request, 'batch_json', {'operation_id': uuid4().hex, 'statements': statements})
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
        worker = Thread(target=invoke, args=(request,), daemon=True)
        workers.append(worker)
        worker.start()
    elif request['kind'] == 'shutdown':
        send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': None})
        break

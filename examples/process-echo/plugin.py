import json
import os
import sys
from threading import Event, Lock, Thread

from .helper import echo


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'external.echo': ['echo', 'wait', 'crash']}
pending, workers = {}, []
output = Lock()


def respond(request, result=None, error=None):
    with output:
        print(json.dumps({'protocol': PROTOCOL, 'id': request['id'], 'ok': error is None,
            'result': result, 'error': error}), flush=True)


def invoke(request, signal):
    if request['method'] == 'crash':
        os._exit(19)
    if request['method'] == 'wait':
        signal.wait()
        respond(request, error='CANCELLED')
    else:
        respond(request, echo(request))


for line in sys.stdin.buffer:
    request = json.loads(line)
    if request['kind'] == 'handshake':
        respond(request, {'protocol': PROTOCOL, 'services': SERVICES,
            'runtime': {'executable': sys.executable, 'python_version': sys.version}})
    elif request['kind'] == 'shutdown':
        for signal in pending.values():
            signal.set()
        for worker in workers:
            worker.join()
        respond(request)
        break
    elif request['kind'] == 'cancel':
        pending[request['target']].set()
    elif request['kind'] == 'invoke':
        signal = pending[request['id']] = Event()
        worker = Thread(target=invoke, args=(request, signal))
        workers.append(worker)
        worker.start()

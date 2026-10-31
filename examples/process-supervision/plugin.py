import json
import os
from pathlib import Path
import sys
from threading import Event, Thread


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'external.supervision': ['idle_exit', 'batch']}
pending = []


def respond(request, result=None):
    print(json.dumps({'protocol': PROTOCOL, 'id': request['id'], 'ok': True,
        'result': result}), flush=True)


def wait_for(path):
    while not Path(path).exists():
        Event().wait(0.005)


def exit_when_released(path):
    wait_for(path)
    os._exit(19)


for line in sys.stdin.buffer:
    request = json.loads(line)
    if request['kind'] == 'handshake':
        respond(request, {'protocol': PROTOCOL, 'services': SERVICES,
            'runtime': {'executable': sys.executable, 'language': 'python', 'version': sys.version}})
    elif request['kind'] == 'shutdown':
        respond(request)
        break
    elif request['kind'] == 'invoke' and request['method'] == 'idle_exit':
        Thread(target=exit_when_released, args=(request['args'][0],), daemon=True).start()
        respond(request, 'armed')
    elif request['kind'] == 'invoke' and request['method'] == 'batch':
        pending.append(request)
        count, ready, release = request['args']
        if len(pending) == count:
            Path(ready).touch()
            wait_for(release)
            for item in pending:
                respond(item, 'completed')
            Event().wait()

import json
import sys


PROTOCOL = 'evidence-plugin-process/1'


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    kind = request['kind']
    result = None
    if kind == 'handshake':
        config = request['configuration']
        result = {'protocol': PROTOCOL, 'services': {'server': ['run']},
                  'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}
    elif kind == 'invoke':
        send({'protocol': PROTOCOL, 'id': 'lookup-' + request['id'], 'kind': 'call',
              'parent': request['id'], 'service': 'source', 'method': 'get', 'args': [{}], 'kwargs': {}})
        callback = json.loads(sys.stdin.readline())
        if not callback['ok']:
            raise RuntimeError(callback['error'])
        result = config['prefix'] + callback['result']
    elif kind == 'cancel':
        continue
    send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': result})
    if kind == 'shutdown':
        break

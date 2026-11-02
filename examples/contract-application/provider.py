import json
import sys


PROTOCOL = 'evidence-plugin-process/1'


for line in sys.stdin:
    request = json.loads(line)
    result = None
    if request['kind'] == 'handshake':
        result = {'protocol': PROTOCOL, 'services': {'value': ['get']},
                  'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}
    elif request['kind'] == 'invoke':
        result = 'world'
    elif request['kind'] == 'cancel':
        continue
    print(json.dumps({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': result}), flush=True)
    if request['kind'] == 'shutdown':
        break

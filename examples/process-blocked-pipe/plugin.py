import json
import sys
from threading import Event


request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({'protocol': 'evidence-plugin-process/1', 'id': request['id'], 'ok': True,
    'result': {'protocol': 'evidence-plugin-process/1', 'services': {'external.blocked': ['accept']},
        'runtime': {'executable': sys.executable, 'language': 'python', 'version': sys.version}}}), flush=True)
Event().wait()

import json
import sys

from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROTOCOL, ProcessTransport


WORKER = '''import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if request["kind"] == "handshake":
        result = {"protocol": request["protocol"], "services": request["capabilities"],
                  "runtime": {"language": "python", "version": sys.version, "executable": sys.executable}}
        print(json.dumps({"protocol": request["protocol"], "id": request["id"], "ok": True, "result": result}), flush=True)
    elif request["kind"] == "invoke":
        with open(sys.argv[1], "a") as effect:
            effect.write(json.dumps({"request_id": request["id"]}) + "\\n")
            effect.flush()
            os.fsync(effect.fileno())
        os._exit(7)
'''


def verify_physical_knowledge(root):
    observations = []
    for fail_intent in (False, True):
        name = 'intent-without-bytes' if fail_intent else 'effect-without-reply'
        source = root / name
        source.mkdir()
        effect, journal = source / 'effects.jsonl', source / 'journal'
        (source / 'worker.py').write_text(WORKER, 'utf-8')
        (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': name,
            'version': '1', 'command': [sys.executable, '-B', '-u', 'worker.py', str(effect)],
            'services': {'fixture': ['invoke']}}), 'utf-8')
        transport = ProcessTransport(source, journal=journal, timeout=3, identity={})
        record, attempted = transport._record, []

        def recording(request, status, **fields):
            if request['kind'] == 'invoke' and status == 'SENT':
                attempted.append(request['id'])
                if fail_intent:
                    raise OSError('Seeded journal failure after sent intent before bytes')
            return record(request, status, **fields)

        try:
            transport.start()
            transport._record = recording
            try:
                transport.request('invoke', service='fixture', method='invoke', args=[{'value': 1}], kwargs={})
            except PluginError as error:
                observed = error.code
            else:
                raise RuntimeError('LOST_RESPONSE_PUBLISHED_SUCCESS')
        finally:
            transport.close()
        business = [row for path in journal.glob('*.json')
                    if (row := json.loads(path.read_text('utf-8')))['request']['kind'] == 'invoke']
        effects = [json.loads(line) for line in effect.read_text('utf-8').splitlines()] if effect.exists() else []
        expected = ['PREPARED', 'UNKNOWN'] if fail_intent else ['PREPARED', 'SENT', 'UNKNOWN']
        statuses = sorted(row['status'] for row in business)
        clean = (transport._closed and transport._process.poll() is not None
                 and not transport._reader.is_alive() and not transport._writer.is_alive()
                 and not transport._supervisor.is_alive())
        if (observed != 'PROCESS_RESULT_UNKNOWN' or statuses != expected or not clean
                or len(attempted) != 1 or len({row['request']['id'] for row in business}) != 1
                or len(effects) != int(not fail_intent)
                or any(row['request_id'] != attempted[0] for row in effects)
                or not fail_intent and transport._process.poll() != 7):
            raise RuntimeError(f'PHYSICAL_KNOWLEDGE_MISMATCH: {name}, {observed}, {statuses}, {effects}, {clean}')
        observations.append({'mode': name, 'decision': 'PASS', 'outcome': observed,
            'statuses': statuses, 'actual_effects': len(effects), 'physical_requests': len(attempted),
            'child_exit': transport._process.poll(), 'io_threads_stopped': clean})
    return observations

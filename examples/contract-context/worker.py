from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event, Lock, Thread, local
from time import monotonic
from uuid import uuid4


package = Path(__file__).resolve().parents[2] / 'kb_platform'
spec = importlib.util.spec_from_file_location('kb_platform', package / '__init__.py',
                                             submodule_search_locations=[str(package)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.knowledge_capabilities import AuthorizedEvidenceClient
from kb_platform.knowledge_context import KnowledgeContext, LocalEvidenceSource, LocalHistorySource
from kb_platform.knowledge_contracts import SessionClient
from kb_platform.plugin_contracts import ContractError


PROTOCOL = 'evidence-plugin-process/1'
SERVICES = {'context': ['metadata', 'prepare_request', 'sources']}
MARKER = 'PROCESS_CONTEXT_PROTOCOL_ACCEPTANCE'
MAX_MESSAGE = 4 * 1024 * 1024
write_lock, state_lock, execution = Lock(), Lock(), local()
pending, retired, active, workers, seen = {}, set(), {}, [], set()
policy = None


def send(value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b'\n'
    if len(payload) > MAX_MESSAGE:
        raise ValueError('PROCESS_MESSAGE_LIMIT')
    with write_lock:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def check():
    if execution.cancelled.is_set():
        raise ContractError('CALL_CANCELLED', 'Context call cancelled', status_code=409)
    if monotonic() >= execution.expires:
        raise ContractError('CALL_DEADLINE_EXCEEDED', 'Context call deadline exceeded', status_code=504)


class Dependency:
    def __init__(self, alias):
        self.alias = alias

    def __getattr__(self, method):
        def invoke(request):
            check()
            identity, response = uuid4().hex, Queue(maxsize=1)
            with state_lock:
                pending[identity] = response
            try:
                send({'protocol': PROTOCOL, 'id': identity, 'kind': 'call',
                      'parent': execution.parent, 'service': self.alias, 'method': method,
                      'args': [request], 'kwargs': {}})
                while True:
                    check()
                    try:
                        value = response.get(timeout=min(0.05, max(0.001, execution.expires - monotonic())))
                    except Empty:
                        continue
                    if value['ok']:
                        return value['result']
                    error = value['error']
                    raise ContractError(error['code'], error['message'],
                        status_code=error.get('status_code', 500), details=error.get('details'))
            finally:
                with state_lock:
                    pending.pop(identity, None)
                    retired.add(identity)
        return invoke


def invoke(request, cancelled):
    execution.parent, execution.cancelled = request['id'], cancelled
    execution.expires = monotonic() + request['deadline_seconds']
    response = {'protocol': PROTOCOL, 'id': request['id'], 'ok': False}
    try:
        check()
        if (request['service'] != 'context' or request['method'] not in SERVICES['context']
                or len(request['args']) != 1 or request['kwargs']):
            raise ContractError('CONTRACT_REQUEST_INVALID', 'Invalid context call', status_code=422)
        values = request['args'][0]
        if request['method'] == 'metadata':
            result = {'policy': asdict(policy)}
        else:
            context = KnowledgeContext(LocalHistorySource(SessionClient(Dependency('sessions'))),
                LocalEvidenceSource(AuthorizedEvidenceClient(Dependency('data'), Dependency('access'))),
                assembler=ContextManager(policy))
            if request['method'] == 'prepare_request':
                values = {**values, 'system': values['system'] + '\n' + MARKER}
            result = getattr(context, request['method'])(values)
        check()
        response.update(ok=True, result=result)
    except Exception as error:
        response['error'] = {'code': getattr(error, 'code', type(error).__name__),
            'message': str(error), 'status_code': getattr(error, 'status_code', 500),
            'details': getattr(error, 'details', {})}
    finally:
        try:
            send(response)
        finally:
            with state_lock:
                active.pop(request['id'], None)


def main():
    global policy
    shutdown = None
    try:
        while line := sys.stdin.buffer.readline(MAX_MESSAGE + 1):
            if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                raise ValueError('PROCESS_MESSAGE_LIMIT')
            request = json.loads(line)
            if request['protocol'] != PROTOCOL:
                raise ValueError('PROCESS_PROTOCOL_MISMATCH')
            if 'ok' in request:
                with state_lock:
                    response = pending.get(request['id'])
                    known = request['id'] in retired
                if response is not None:
                    response.put_nowait(request)
                elif not known:
                    raise ValueError('PROCESS_CALLBACK_UNKNOWN')
                continue
            if request['id'] in seen:
                raise ValueError('PROCESS_DUPLICATE_REQUEST')
            seen.add(request['id'])
            kind = request['kind']
            if kind == 'handshake' and policy is None:
                if request['capabilities'] != SERVICES:
                    raise ValueError('PROCESS_CAPABILITY_MISMATCH')
                policy = ContextPolicy(**request['configuration'])
                send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True,
                    'result': {'protocol': PROTOCOL, 'services': SERVICES,
                        'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}})
            elif kind == 'invoke' and policy is not None:
                signal = Event()
                if request['cancelled']:
                    signal.set()
                with state_lock:
                    active[request['id']] = signal
                worker = Thread(target=invoke, args=(request, signal))
                workers.append(worker)
                worker.start()
            elif kind == 'cancel' and policy is not None:
                with state_lock:
                    signal = active.get(request['target'])
                if signal is not None:
                    signal.set()
            elif kind == 'shutdown' and policy is not None:
                shutdown = request['id']
                break
            else:
                raise ValueError('PROCESS_INVALID_STATE')
    finally:
        with state_lock:
            for signal in active.values():
                signal.set()
        for worker in workers:
            worker.join()
    if shutdown is not None:
        send({'protocol': PROTOCOL, 'id': shutdown, 'ok': True, 'result': None})


if __name__ == '__main__':
    main()

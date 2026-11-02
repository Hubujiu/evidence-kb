from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Lock, Thread
from urllib.parse import urlsplit


class LoopbackServer:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.requests, self.errors = [], []
        self.lock = Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                request = {'method': self.command, 'path': urlsplit(self.path).path,
                           'headers': {key.lower(): value for key, value in self.headers.items()},
                           'body': body}
                with fixture.lock:
                    fixture.requests.append(request)
                    try:
                        response = fixture.dispatch(deepcopy(request))
                    except Exception as error:
                        fixture.errors.append(repr(error))
                        response = (500, b'fixture rejected request', {})
                if response is None:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, content, headers = response
                self.send_response(status)
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Connection', 'close')
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.close_connection = True
                if self.command != 'HEAD':
                    self.wfile.write(content)

            do_POST = handle_request
            do_PUT = handle_request
            do_GET = handle_request
            do_HEAD = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class LocalModelWire(LoopbackServer):
    def __init__(self, adapter, responses):
        self.adapter, self.responses = adapter, list(responses)
        super().__init__(self.respond)

    def respond(self, request):
        expected = '/v1/messages' if self.adapter == 'anthropic' else '/v1/chat/completions'
        if request['method'] != 'POST' or request['path'] != expected:
            raise ValueError('UNEXPECTED_MODEL_WIRE')
        if not self.responses:
            raise ValueError('UNEXPECTED_ADDITIONAL_MODEL_ATTEMPT')
        payload = json.loads(request['body'])
        response = self.responses.pop(0)
        if response is None:
            return None
        messages = payload['messages']
        if self.adapter == 'anthropic' and 'system' in payload:
            messages = [{'role': 'system', 'content': payload['system']}, *messages]
        value = response(messages) if callable(response) else response
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if self.adapter == 'anthropic':
            result = {'id': 'local-observation', 'type': 'message', 'role': 'assistant',
                      'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
                      'usage': {'input_tokens': 10, 'output_tokens': 5}}
        else:
            result = {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
                      'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        return 200, json.dumps(result).encode(), {'Content-Type': 'application/json'}


class LocalS3Wire(LoopbackServer):
    def __init__(self):
        self.objects = {}
        super().__init__(self.respond)

    def respond(self, request):
        key, method = request['path'], request['method']
        if not key.startswith('/foundation-fixture/'):
            raise ValueError('UNEXPECTED_S3_BUCKET')
        if method == 'PUT':
            if request['headers'].get('if-none-match') == '*' and key in self.objects:
                return 412, b'<Error><Code>PreconditionFailed</Code></Error>', {'Content-Type': 'application/xml'}
            metadata = {name: value for name, value in request['headers'].items()
                        if name.startswith('x-amz-meta-')}
            metadata['ETag'] = '"' + sha256(request['body']).hexdigest() + '"'
            self.objects[key] = (request['body'], metadata)
            return 200, b'', metadata
        if method not in {'GET', 'HEAD'}:
            raise ValueError('UNEXPECTED_S3_OPERATION')
        if key not in self.objects:
            return 404, b'<Error><Code>NoSuchKey</Code></Error>', {'Content-Type': 'application/xml'}
        return 200, *self.objects[key]

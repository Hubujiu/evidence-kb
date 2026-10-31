import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';

const protocol = 'evidence-plugin-process/1';
const services = { 'external.consumer': ['invoke', 'denied', 'duplicate', 'unknown_parent'] };
const pending = new Map();
const input = createInterface({ input: process.stdin });
const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);

function call(request, service, prompt) {
  return new Promise((resolve) => {
    const id = randomUUID();
    pending.set(id, resolve);
    const frame = {
      protocol, id, kind: 'call', parent: request.id,
      service, method: 'generate', args: [prompt], kwargs: {},
    };
    if (request.method === 'unknown_parent') frame.parent = 'unrelated-request';
    send(frame);
    if (request.method === 'duplicate') send(frame);
  });
}

async function invoke(request) {
  const service = request.method === 'denied' ? 'private.model' : 'shared.model';
  const first = await call(request, service, request.args[0]);
  const result = first.ok ? await call(request, service, `${first.result.answer}:continued`) : first;
  send({ protocol, id: request.id, ok: true, result });
}

input.on('line', (line) => {
  const request = JSON.parse(line);
  if ('ok' in request) {
    pending.get(request.id)(request);
    pending.delete(request.id);
  } else if (request.kind === 'handshake') {
    send({ protocol, id: request.id, ok: true, result: {
      protocol, services,
      runtime: { language: 'javascript', version: process.version, executable: process.execPath },
    } });
  } else if (request.kind === 'invoke') {
    void invoke(request);
  } else if (request.kind === 'shutdown') {
    send({ protocol, id: request.id, ok: true, result: null });
    input.close();
    process.stdin.pause();
  }
});

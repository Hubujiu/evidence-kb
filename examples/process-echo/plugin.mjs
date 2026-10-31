import { createInterface } from 'node:readline';

const protocol = 'evidence-plugin-process/1';
const services = { 'external.echo': ['echo', 'wait', 'crash'] };
const pending = new Map();
const input = createInterface({ input: process.stdin });

function respond(request, result = null, error = null) {
  process.stdout.write(`${JSON.stringify({ protocol, id: request.id, ok: error === null, result, error })}\n`);
}

input.on('line', (line) => {
  const request = JSON.parse(line);
  if (request.kind === 'handshake') {
    respond(request, {
      protocol,
      services,
      runtime: { language: 'javascript', version: process.version, executable: process.execPath },
    });
  } else if (request.kind === 'shutdown') {
    for (const call of pending.values()) respond(call, null, 'CANCELLED');
    pending.clear();
    respond(request);
    input.close();
    process.stdin.pause();
  } else if (request.kind === 'cancel') {
    const call = pending.get(request.target);
    if (call) respond(call, null, 'CANCELLED');
    pending.delete(request.target);
  } else if (request.kind === 'invoke') {
    if (request.method === 'crash') process.exit(19);
    if (request.method === 'wait') {
      pending.set(request.id, request);
    } else {
      respond(request, {
        args: request.args,
        kwargs: request.kwargs,
        operation: request.identity.operation,
      });
    }
  }
});

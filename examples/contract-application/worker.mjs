import readline from 'node:readline';

const protocol = 'evidence-plugin-process/1';
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
const pending = new Map();
let config;
for await (const line of readline.createInterface({input: process.stdin})) {
  const request = JSON.parse(line);
  if (pending.has(request.id)) {
    const parent = pending.get(request.id);
    pending.delete(request.id);
    send({protocol, id: parent, ok: request.ok,
      result: request.ok ? config.prefix + request.result : null, error: request.error});
    continue;
  }
  let result = null;
  if (request.kind === 'handshake') {
    config = request.configuration;
    result = {protocol, services: {server: ['run']},
      runtime: {language: 'javascript', version: process.version, executable: process.execPath}};
  } else if (request.kind === 'invoke') {
    const id = 'lookup-' + request.id;
    pending.set(id, request.id);
    send({protocol, id, kind: 'call', parent: request.id,
      service: 'source', method: 'get', args: [{}], kwargs: {}});
    continue;
  } else if (request.kind === 'cancel') {
    continue;
  }
  send({protocol, id: request.id, ok: true, result});
  if (request.kind === 'shutdown') break;
}

import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
import { OperationOwner } from '../web/src/operation-owner.ts';

test('current operation can commit', () => {
 const owner = new OperationOwner();
 const operation = owner.begin(() => true);
 let writes = 0;
 assert.equal(operation.commit(() => writes++), true);
 assert.equal(writes, 1);
});

test('supersession aborts transport and rejects stale result and error writes', () => {
 const owner = new OperationOwner();
 const previous = owner.begin(() => true);
 const current = owner.begin(() => true);
 assert.equal(previous.signal.aborted, true);
 assert.equal(previous.commit(() => assert.fail('stale result')), false);
 assert.equal(previous.commit(() => assert.fail('stale error')), false);
 assert.equal(current.isCurrent(), true);
});

test('identity changes invalidate even an abort-ignoring response body', async () => {
 const owner = new OperationOwner();
 let epoch = 1;
 const captured = epoch;
 const operation = owner.begin(() => epoch === captured);
 let release;
 const body = new Promise(resolve => { release = resolve; });
 const pending = body.then(() => operation.commit(() => assert.fail('old identity wrote')));
 epoch++;
 release();
 assert.equal(await pending, false);
});

test('returning to the same token does not restore the previous identity epoch', () => {
 const owner = new OperationOwner();
 let epoch = 1;
 let token = 'A';
 const captured = epoch;
 const operation = owner.begin(() => epoch === captured && token === 'A');
 epoch++; token = 'B';
 epoch++; token = 'A';
 assert.equal(operation.isCurrent(), false);
});

test('scope and page changes reject old callbacks before effect cleanup', () => {
 const owner = new OperationOwner();
 let scope = 'old';
 let page = 'documents';
 const operation = owner.begin(() => scope === 'old' && page === 'documents');
 scope = 'new';
 assert.equal(operation.isCurrent(), false);
 scope = 'old'; page = 'chat';
 assert.equal(operation.commit(() => assert.fail('unmounted page wrote')), false);
});

test('unmount invalidation blocks late resource allocation', async () => {
 const owner = new OperationOwner();
 const operation = owner.begin(() => true);
 let allocated = 0;
 const pending = Promise.resolve().then(() => operation.commit(() => allocated++));
 owner.invalidate();
 assert.equal(await pending, false);
 assert.equal(allocated, 0);
 assert.equal(operation.signal.aborted, true);
});

test('old completion cannot clear a newer operation', () => {
 const owner = new OperationOwner();
 const old = owner.begin(() => true);
 const current = owner.begin(() => true);
 let busy = true;
 old.commit(() => { busy = false; });
 assert.equal(busy, true);
 current.commit(() => { busy = false; });
 assert.equal(busy, false);
});


test('restoring a previous scope or page never restores its old operation generation', () => {
 const owner = new OperationOwner();
 let scopeEpoch = 1;
 let viewEpoch = 1;
 const operation = owner.begin(() => scopeEpoch === 1 && viewEpoch === 1);
 scopeEpoch += 2;
 assert.equal(operation.commit(() => assert.fail('scope ABA wrote')), false);
 const pageOperation = owner.begin(() => viewEpoch === 1);
 viewEpoch += 2;
 assert.equal(pageOperation.commit(() => assert.fail('view ABA wrote')), false);
});

function publishFixture() {
 const elements = new Map();
 const requests = [];
 const commits = [];
 const pending = new Map();
 const element = id => {
  if (!elements.has(id)) {
   let text = '';
   const node = { value: '', disabled: false, children: [],
    replaceChildren() { this.children = []; if (id === 'kb') this.value = ''; },
    append(child) { this.children.push(child); if (id === 'kb' && !this.value) this.value = child.value; },
   };
   Object.defineProperty(node, 'textContent', {
    get: () => text,
    set: value => { text = value; commits.push({id, scope: element('kb').value, value}); },
   });
   elements.set(id, node);
  }
  return elements.get(id);
 };
 element('kb').value = 'scope';
 element('token').value = 'fixture-token';
 const context = vm.createContext({
  document: { getElementById: element, createElement: () => ({value: '', textContent: ''}) },
  URL, FormData, setTimeout,
  fetch: async (path, options = {}) => {
   requests.push({path, method: options.method || 'GET', scope: element('kb').value});
   return {ok: true, json: () => new Promise(resolve => pending.set(path, resolve))};
  },
 });
 vm.runInContext(readFileSync(new URL('../kb_platform/console.mjs', import.meta.url), 'utf8'), context);
 const drain = () => new Promise(resolve => setImmediate(resolve));
 const resolve = (path, value) => { assert.ok(pending.has(path), path); pending.get(path)(value); };
 return {element, requests, commits, resolve, drain};
}

async function prepareConcurrentCreate(fixture) {
 const creating = fixture.element('create').onclick();
 await fixture.drain();
 fixture.resolve('/knowledge-bases', {kb_id: 'next'});
 await fixture.drain();
 return {creating};
}

test('publish never starts its second mutation after a concurrent scope selection', async () => {
 const fixture = publishFixture();
 const publishing = fixture.element('publish').onclick();
 await fixture.drain();
 const {creating} = await prepareConcurrentCreate(fixture);
 fixture.resolve('/knowledge-bases', [{kb_id: 'scope', name: 'Old'}, {kb_id: 'next', name: 'New'}]);
 fixture.resolve('/knowledge-bases/scope/builds', {build_id: 'build-old'});
 await fixture.drain();
 assert.deepEqual(fixture.requests.filter(request => request.path.endsWith('/publish')), []);
 await Promise.all([creating, publishing]);
 assert.equal(fixture.element('message').textContent, '已创建知识库。');
 assert.equal(fixture.element('publish').disabled, false);
});

test('publish acknowledgement from an older scope cannot overwrite the current status', async () => {
 const fixture = publishFixture();
 const publishing = fixture.element('publish').onclick();
 await fixture.drain();
 fixture.resolve('/knowledge-bases/scope/builds', {build_id: 'build-old'});
 await fixture.drain();
 const {creating} = await prepareConcurrentCreate(fixture);
 fixture.resolve('/knowledge-bases', [{kb_id: 'scope', name: 'Old'}, {kb_id: 'next', name: 'New'}]);
 fixture.resolve('/knowledge-bases/scope/builds/build-old/publish', {manifest_id: 'old-manifest'});
 await Promise.all([creating, publishing]);
 assert.deepEqual(fixture.commits.filter(commit => commit.value.includes('old-manifest')), []);
 assert.equal(fixture.element('message').textContent, '已创建知识库。');
 assert.equal(fixture.requests.filter(request => request.path.endsWith('/publish')).length, 1);
});

test('current publish renders its acknowledged manifest after exactly one mutation', async () => {
 const fixture = publishFixture();
 const publishing = fixture.element('publish').onclick();
 await fixture.drain();
 fixture.resolve('/knowledge-bases/scope/builds', {build_id: 'build-old'});
 await fixture.drain();
 fixture.resolve('/knowledge-bases/scope/builds/build-old/publish', {manifest_id: 'current-manifest'});
 await publishing;
 assert.equal(fixture.element('message').textContent, '索引已发布。版本 current-manifest');
 assert.equal(fixture.requests.filter(request => request.path.endsWith('/publish')).length, 1);
 assert.equal(fixture.element('publish').disabled, false);
});

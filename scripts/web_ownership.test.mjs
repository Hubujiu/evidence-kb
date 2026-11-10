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

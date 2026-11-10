import assert from 'node:assert/strict';
import test from 'node:test';
import { OperationOwner } from '../web/src/operation-owner.ts';
import { canParkTurn, canSendTurn, canStartTurn, isPreHandlerRejection, requestTransition, turnTransition } from '../web/src/turn-request.ts';

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

const turnRequest = { identityEpoch: 4, scopeId: 'scope', sessionId: 'session', requestId: 'original-request', question: 'Preserve this question', draftOwner: 7 };
const startTurn = request => turnTransition({phase: 'idle'}, {type: 'start', request});

test('session creation has no submitted request until its own success', () => {
 const state = startTurn({...turnRequest, sessionId: null});
 assert.equal(state.phase, 'creating');
 assert.equal(state.request.question, 'Preserve this question');
 assert.equal(canStartTurn(state), false);
 assert.equal(turnTransition(state, {type: 'creation_failed', requestId: turnRequest.requestId}).phase, 'idle');
 const created = turnTransition(state, {type: 'created', requestId: turnRequest.requestId, sessionId: 'new-session'});
 assert.equal(created.phase, 'submitting');
 assert.equal(created.request.sessionId, 'new-session');
 assert.equal(created.request.requestId, 'original-request');
});

test('a context change before the POST does not invent an uncertain send', () => {
 const state = startTurn(turnRequest);
 assert.equal(turnTransition(state, {type: 'not_submitted', requestId: turnRequest.requestId}).phase, 'idle');
});

test('unconfirmed and every nonterminal recovery outcome retain identity and block new turns', () => {
 const state = turnTransition(startTurn(turnRequest), {type: 'unconfirmed', requestId: turnRequest.requestId, detail: 'Response lost'});
 for (const status of ['unknown', 'running', 'not_found', 'interrupted', 'reconciliation_required']) {
  const checking = turnTransition(state, {type: 'check', requestId: turnRequest.requestId});
  const observed = turnTransition(checking, {type: 'observed', requestId: turnRequest.requestId, status, detail: status});
  assert.equal(observed.phase, 'unconfirmed');
  assert.deepEqual(observed.request, turnRequest);
  assert.equal(canStartTurn(observed), false);
  assert.equal(turnTransition(observed, {type: 'start', request: {...turnRequest, requestId: 'duplicate'}}), observed);
 }
});

test('a failed recovery read cannot release or replace the original request', () => {
 const uncertain = turnTransition(startTurn(turnRequest), {type: 'unconfirmed', requestId: turnRequest.requestId, detail: 'Unknown'});
 const checking = turnTransition(uncertain, {type: 'check', requestId: turnRequest.requestId});
 const failed = turnTransition(checking, {type: 'check_failed', requestId: turnRequest.requestId, detail: 'Read failed'});
 assert.equal(failed.phase, 'unconfirmed');
 assert.deepEqual(failed.request, turnRequest);
 assert.equal(canStartTurn(failed), false);
});

test('receiving a business error still confirms the stored request result', () => {
 const state = turnTransition(startTurn(turnRequest), {type: 'received', requestId: turnRequest.requestId, response: {status: 'error', answer: ''}});
 assert.equal(state.phase, 'confirmed');
 assert.equal(state.response.status, 'error');
 assert.equal(turnTransition(state, {type: 'unconfirmed', requestId: turnRequest.requestId, detail: 'Later GET failed'}), state);
});

test('refresh failure preserves confirmed answer and requires a safe session refresh', () => {
 const answer = {status: 'answered', answer: 'Known answer'};
 const confirmed = turnTransition(startTurn(turnRequest), {type: 'received', requestId: turnRequest.requestId, response: answer});
 const refreshing = turnTransition(confirmed, {type: 'refresh', requestId: turnRequest.requestId});
 const failed = turnTransition(refreshing, {type: 'refresh_failed', requestId: turnRequest.requestId, detail: 'Read unavailable'});
 assert.equal(failed.phase, 'refresh_required');
 assert.equal(failed.response, answer);
 assert.equal(canStartTurn(failed), false);
 const retryRead = turnTransition(failed, {type: 'refresh', requestId: turnRequest.requestId});
 const synced = turnTransition(retryRead, {type: 'synced', requestId: turnRequest.requestId, detail: 'List unavailable'});
 assert.equal(synced.phase, 'complete');
 assert.equal(synced.response, answer);
 assert.equal(canStartTurn(synced), true);
});

test('recovery can confirm a redacted result without pretending a new model send', () => {
 let state = startTurn(turnRequest);
 state = turnTransition(state, {type: 'unconfirmed', requestId: turnRequest.requestId, detail: 'Unknown'});
 state = turnTransition(state, {type: 'check', requestId: turnRequest.requestId});
 state = turnTransition(state, {type: 'received', requestId: turnRequest.requestId, response: {status: 'redacted'}});
 assert.equal(state.phase, 'confirmed');
 assert.equal(state.request.requestId, 'original-request');
 assert.deepEqual(state.response, {status: 'redacted'});
});

test('a stale completion cannot mutate a later request or an identity reset', () => {
 const state = startTurn({...turnRequest, requestId: 'new-request'});
 assert.equal(turnTransition(state, {type: 'received', requestId: 'original-request', response: {answer: 'Stale'}}), state);
 const reset = turnTransition(state, {type: 'reset'});
 assert.equal(turnTransition(reset, {type: 'received', requestId: 'new-request', response: {answer: 'Stale'}}), reset);
});

test('definite rejection is separate from an unknown or already received result', () => {
 const state = startTurn(turnRequest);
 const rejected = turnTransition(state, {type: 'rejected', requestId: turnRequest.requestId, detail: 'Pre-handler delivery guard'});
 assert.equal(rejected.phase, 'rejected');
 assert.equal(canStartTurn(rejected), true);
 const confirmed = turnTransition(state, {type: 'received', requestId: turnRequest.requestId, response: {answer: 'Known'}});
 assert.equal(turnTransition(confirmed, {type: 'rejected', requestId: turnRequest.requestId, detail: 'Late false'}), confirmed);
 const unknown = turnTransition(state, {type: 'unconfirmed', requestId: turnRequest.requestId, detail: 'Uncertain'});
 assert.equal(turnTransition(unknown, {type: 'rejected', requestId: turnRequest.requestId, detail: 'Late false'}), unknown);
});

test('only the exact pre-handler recovery guard proves this whole turn was not admitted', () => {
 const guard = {code: 'DELIVERY_RECOVERY_REQUIRED', mode: 'restricted_recovery', reason: 'UNRESOLVED_PHYSICAL_REQUEST', model_sent: false, automatic_retry: false};
 assert.equal(isPreHandlerRejection(guard), true);
 for (const omitted of Object.keys(guard)) {
  const incomplete = {...guard}; delete incomplete[omitted];
  assert.equal(isPreHandlerRejection(incomplete), false);
 }
 for (const facts of [{code: 'COMMIT_UNKNOWN', model_sent: false, budget_operation_id: 'budget-operation', recovery_action: 'inspect_operation'}, {model_sent: false}, {code: 'DELIVERY_RECOVERY_REQUIRED', model_sent: false}, {...guard, operation_id: 'operation'}, {...guard, outcome: 'UNKNOWN'}, {...guard, ledger_settlement: 'UNCONFIRMED'}, {...guard, capture_state: 'UNCONFIRMED'}, {...guard, recovery_action: 'inspect_operation'}]) {
  assert.equal(isPreHandlerRejection(facts), false);
 }
});

const unresolvedRequest = (request, status = 'interrupted') => {
 let state = turnTransition(startTurn(request), {type: 'unconfirmed', requestId: request.requestId, detail: 'Unknown', facts: {code: 'COMMIT_UNKNOWN', model_sent: false, budget_operation_id: 'original-operation', recovery_action: 'inspect_operation'}});
 state = turnTransition(state, {type: 'check', requestId: request.requestId});
 return turnTransition(state, {type: 'observed', requestId: request.requestId, status, detail: status});
};

test('parking retains original identity and error facts while allowing only a distinct session', () => {
 for (const status of ['interrupted', 'not_found', 'reconciliation_required']) {
  const original = unresolvedRequest(turnRequest, status);
  const parked = requestTransition({active: original, retained: []}, {type: 'park', requestId: turnRequest.requestId});
  assert.equal(parked.active.phase, 'idle');
  assert.deepEqual(parked.retained, [original]);
  assert.equal(canSendTurn(parked, null), true);
  assert.equal(canSendTurn(parked, 'different-session'), true);
  assert.equal(canSendTurn(parked, turnRequest.sessionId), false);
  assert.equal(requestTransition(parked, {type: 'start', request: {...turnRequest, requestId: 'unsafe-retry'}}), parked);
  assert.equal(requestTransition(parked, {type: 'start', request: {...turnRequest, sessionId: null}}), parked);
 }
 for (const status of ['unknown', 'running']) {
  const original = unresolvedRequest(turnRequest, status);
  const state = {active: original, retained: []};
  assert.equal(canParkTurn(original), false);
  assert.equal(requestTransition(state, {type: 'park', requestId: turnRequest.requestId}), state);
 }
});

test('each retained recovery updates only its own record and never replaces the active turn', () => {
 let state = requestTransition({active: unresolvedRequest(turnRequest), retained: []}, {type: 'park', requestId: turnRequest.requestId});
 const second = {...turnRequest, sessionId: 'second-session', requestId: 'second-request'};
 state = requestTransition(state, {type: 'start', request: second});
 state = requestTransition(state, {type: 'unconfirmed', requestId: second.requestId, detail: 'Second unknown'});
 state = requestTransition(state, {type: 'check', requestId: second.requestId});
 state = requestTransition(state, {type: 'observed', requestId: second.requestId, status: 'interrupted', detail: 'Still interrupted'});
 state = requestTransition(state, {type: 'park', requestId: second.requestId});
 assert.deepEqual(state.retained.map(attempt => attempt.request.requestId), [turnRequest.requestId, second.requestId]);
 const third = {...turnRequest, sessionId: 'third-session', requestId: 'third-request'};
 state = requestTransition(state, {type: 'start', request: third});
 const active = state.active;
 state = requestTransition(state, {type: 'check', requestId: turnRequest.requestId});
 state = requestTransition(state, {type: 'received', requestId: turnRequest.requestId, response: {answer: 'Recovered original'}});
 assert.equal(state.active, active);
 assert.equal(state.retained[0].phase, 'confirmed');
 assert.equal(state.retained[0].facts.budget_operation_id, 'original-operation');
 assert.equal(state.retained[1].phase, 'unconfirmed');
 assert.equal(canSendTurn({...state, active: {phase: 'idle'}}, turnRequest.sessionId), false);
 state = requestTransition(state, {type: 'refresh', requestId: turnRequest.requestId});
 state = requestTransition(state, {type: 'synced', requestId: turnRequest.requestId, detail: ''});
 assert.equal(canSendTurn({...state, active: {phase: 'idle'}}, turnRequest.sessionId), true);
 assert.equal(state.active, active);
 const reset = requestTransition(state, {type: 'reset'});
 assert.deepEqual(reset, {active: {phase: 'idle'}, retained: []});
 assert.deepEqual(requestTransition(reset, {type: 'received', requestId: turnRequest.requestId, response: {answer: 'Late'}}), reset);
});

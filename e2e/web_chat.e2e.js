import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

test('connect, ask, verify citation and restore a conversation', async ({app, screen, browser}) => {
  await app.open('/');
  await screen.getByRole('button', '连接知识库').tap();
  await screen.getByLabel('访问令牌').fill('bad-token');
  await screen.getByRole('button', '连接知识库').tap();
  await expect(screen.getByRole('alert')).toContainText('身份验证失败');
  await screen.getByLabel('访问令牌').fill('web-e2e-token');
  await screen.getByRole('button', '连接知识库').tap();
  await expect(screen.getByRole('button', '切换连接')).toBeVisible();
  await expect(screen.getByLabel('选择知识库')).toContainText('测试知识库');
  await browser.locator('.chat-options summary').tap();
  await screen.getByLabel('来源范围').selectOption({value: 'LOCKED'});
  await screen.getByLabel('选择来源（可多选）').selectOption({value: 's0'});
  await screen.getByLabel('文档版本').selectOption({value: 'pinned'});
  await screen.getByLabel('必须遵守的要求（每行一条）').fill('使用简短回答');
  await screen.getByLabel('向知识库提问').fill('What is the original rule?');
  await screen.getByRole('button', '发送问题').tap();
  await expect(screen.getByText('Original rule: value 42.')).toBeVisible();
  await expect(screen.getByRole('button', '1. Original.md')).toBeVisible();
  await screen.getByRole('button', '1. Original.md').tap();
  await expect(screen.getByRole('dialog')).toContainText('原文预览');
  await expect(browser.frameLocator('iframe[title="原文内容"]').getByText('Original rule: value 42.')).toBeVisible();
  await screen.getByRole('button', '关闭原文预览').tap();
  await screen.getByRole('button', '新建对话').tap();
  await expect(screen.getByText('从一个问题开始')).toBeVisible();
  await screen.getByRole('button', '测试知识库 · 第 1 轮').tap();
  await expect(screen.getByText('What is the original rule?')).toBeVisible();
  const restored = await browser.evaluate(() => ({mode: document.querySelector('#source-mode').value, source: document.querySelector('#source-ids').value, version: document.querySelector('#source-version').value, constraint: document.querySelector('#chat-constraints').value}));
  expect(restored).toEqual({mode: 'LOCKED', source: 's0', version: 'pinned', constraint: '使用简短回答'});
  await expect(screen.getByText('Original rule: value 42.')).toBeVisible();
  await screen.getByLabel('向知识库提问').fill('And the rule?');
  await screen.getByRole('button', '发送问题').tap();
  await expect(screen.getByRole('button', '测试知识库 · 第 2 轮')).toBeVisible();
  const evidence = await browser.locator('.citation').count();
  expect(evidence).toBe(2);
});

async function recoveryFixture(app, screen, browser, settings = {}) {
 await app.open('/');
 await browser.evaluate(config => {
  const posts = [], reads = [], readSessions = [], created = [];
  let recoveryStatus = config.permanentInterrupted ? 'interrupted' : 'running';
  let mismatched = false;
  const policy = {mode: 'AUTO', source_ids: [], versions: {}, version_policy: 'latest', lifetime: 'session'};
  const session = (id = 'recover-session') => ({session_id: id, scope_id: 'scope', revision: id === 'recover-session' && ((config.lostResponse && recoveryStatus !== 'completed') || config.rejected || config.commitUnknown) ? 0 : posts.filter(post => post.session_id === id).length, state: {source_policy: policy}});
  const result = (id = 'recover-session') => ({session_id: id, revision: 1, status: 'answered', answer: id === 'recover-session' ? 'Confirmed original answer' : 'Confirmed new-session answer', evidence: [], claims: [], unresolved: []});
  const redacted = () => ({status: 'redacted'});
  const ready = value => Promise.resolve({ok: true, status: 200, json: async () => value});
  window.fetch = (path, options = {}) => {
   if (path === '/scopes') return ready([{scope_id: 'scope', name: 'Recovery scope'}, {scope_id: 'other', name: 'Other scope'}]);
   if (path === '/capabilities') return ready({agent_conversations: {configured: true}});
   if (path.endsWith('/agent-sources')) return ready([]);
   if (path === '/agent/sessions?limit=50') {
    if (config.listFailure && posts.length) return Promise.reject(Error('List refresh unavailable'));
    return ready({sessions: created.map(id => session(id))});
   }
   if (path === '/scopes/scope/sessions') {
    if (config.creationFailure) return Promise.reject(Error('Session creation unavailable'));
    const id = created.length ? `distinct-session-${created.length + 1}` : 'recover-session';
    created.push(id); return ready(session(id));
   }
   if (path.endsWith('/turns')) {
    const id = decodeURIComponent(path.split('/')[3]);
    posts.push({...JSON.parse(options.body), session_id: id});
    if (config.rejected) return Promise.resolve({ok: false, status: 503, json: async () => ({error: 'DELIVERY_RECOVERY_REQUIRED', mode: 'restricted_recovery', reason: 'UNRESOLVED_PHYSICAL_REQUEST', model_sent: false, automatic_retry: false, allowed_actions: ['inspect_request', 'inspect_history', 'export_session', 'reconcile_ledger']})});
    if (config.commitUnknown) return Promise.resolve({ok: false, status: 503, json: async () => ({error: 'COMMIT_UNKNOWN', detail: 'Reservation is unresolved', model_sent: false, budget_operation_id: 'budget-operation', outcome: 'UNKNOWN', ledger_settlement: 'UNCONFIRMED', recovery_action: 'inspect_operation'})});
    return config.lostResponse && id === 'recover-session' ? Promise.reject(Error('Turn response lost')) : ready(result(id));
   }
   if (path.includes('/requests/')) {
    reads.push(decodeURIComponent(path.split('/requests/')[1]));
    readSessions.push(decodeURIComponent(path.split('/')[3]));
    const identity = {session_id: 'recover-session', request_id: mismatched ? 'different-request' : reads.at(-1), revision: session().revision, send_status: 'unknown'};
    const actions = {running: 'wait', interrupted: 'inspect_request', not_found: 'verify_request_id', reconciliation_required: 'inspect_operation'};
    return ready(recoveryStatus === 'completed' ? {...identity, status: recoveryStatus, recovery_action: config.redacted ? 'inspect_request' : 'retrieve_result', response: config.redacted ? redacted() : result()}
     : {...identity, status: recoveryStatus, recovery_action: actions[recoveryStatus]});
   }
   if (path.endsWith('/history')) {
    const id = decodeURIComponent(path.split('/')[3]);
    return ready({events: posts.filter(post => post.session_id === id).flatMap(post => [{kind: 'user', group_id: post.request_id, payload: {text: post.message}}, ...(id === 'recover-session' && config.lostResponse && recoveryStatus !== 'completed' ? [] : [{kind: 'turn_completed', group_id: post.request_id, payload: config.redacted ? redacted() : result(id)}])])});
   }
   if (path.startsWith('/agent/sessions/')) return config.sessionFailure ? Promise.reject(Error('Session refresh unavailable')) : ready(session(decodeURIComponent(path.split('/')[3])));
   throw Error('Unexpected recovery fixture request: ' + path);
  };
  window.turnRecovery = {posts, reads, readSessions, status: value => {if (config.permanentInterrupted) throw Error('The interrupted backend request never completes'); recoveryStatus = value;}, mismatch: value => {mismatched = value;}, allowSession: () => {config.sessionFailure = false;}};
 }, settings);
 await screen.getByRole('button', '连接知识库').tap();
 await screen.getByLabel('访问令牌').fill('synthetic-recovery-token');
 await screen.getByRole('button', '连接知识库').tap();
 await expect(screen.getByRole('button', '切换连接')).toBeVisible();
 await screen.getByLabel('向知识库提问').fill('Keep the original question');
 await screen.getByRole('button', '发送问题').tap();
}

test('failed implicit session creation preserves the unsent question', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {creationFailure: true});
 await expect(screen.getByRole('alert')).toContainText('会话未建立，问题已保留');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Keep the original question');
 expect(await browser.evaluate(() => window.turnRecovery.posts.length)).toBe(0);
 expect(await browser.locator('.request-recovery').count()).toBe(0);
});

test('known answer survives session refresh failure and a manual read restores safe continuation', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {sessionFailure: true});
 await expect(browser.locator('.messages')).toContainText('Confirmed original answer');
 await expect(browser.locator('.request-recovery')).toContainText('结果已确认，等待刷新会话');
 await screen.getByLabel('向知识库提问').fill('A later draft');
 expect(await browser.evaluate(() => document.querySelector('[aria-label="发送问题"]').disabled)).toBe(true);
 await browser.evaluate(() => window.turnRecovery.allowSession());
 await screen.getByRole('button', '刷新原会话').tap();
 await expect(screen.getByRole('button', 'Recovery scope · 第 1 轮')).toBeVisible();
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('A later draft');
 expect(await browser.evaluate(() => ({posts: window.turnRecovery.posts.length, recoveryReads: window.turnRecovery.reads.length, sendDisabled: document.querySelector('[aria-label="发送问题"]').disabled}))).toEqual({posts: 1, recoveryReads: 0, sendDisabled: false});
});

test('list refresh failure never relabels a received result as an uncertain turn', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {listFailure: true});
 await expect(browser.locator('.messages')).toContainText('Confirmed original answer');
 await expect(screen.getByRole('alert')).toContainText('本轮结果已确认；最近对话列表暂未刷新');
 await screen.getByLabel('向知识库提问').fill('The next question');
 expect(await browser.locator('.request-recovery').count()).toBe(0);
 expect(await browser.evaluate(() => document.querySelector('[aria-label="发送问题"]').disabled)).toBe(false);
 expect(await browser.evaluate(() => window.turnRecovery.posts.length)).toBe(1);
});

test('original request recovery keeps every unresolved state blocked without resending', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {lostResponse: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await screen.getByLabel('向知识库提问').fill('Preserve the next draft');
 const original = await browser.evaluate(() => window.turnRecovery.posts[0].request_id);
 for (const [status, text] of [['running', '原请求仍在执行'], ['not_found', '尚未查到原请求记录'], ['interrupted', '原请求已中断'], ['reconciliation_required', '需要维护者核对']]) {
  await browser.evaluate(value => window.turnRecovery.status(value), status);
  await screen.getByRole('button', '核对原请求').tap();
  await expect(browser.locator('.request-recovery')).toContainText(text);
  expect(await browser.evaluate(() => ({posts: window.turnRecovery.posts.length, blocked: document.querySelector('[aria-label="发送问题"]').disabled}))).toEqual({posts: 1, blocked: true});
 }
 expect(await browser.evaluate(() => window.turnRecovery.reads)).toEqual([original, original, original, original]);
 expect(await browser.evaluate(() => window.turnRecovery.readSessions)).toEqual(['recover-session', 'recover-session', 'recover-session', 'recover-session']);
});

test('a running request can complete and restore its original view without clearing a later draft', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {lostResponse: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await screen.getByRole('button', '核对原请求').tap();
 await expect(browser.locator('.request-recovery')).toContainText('原请求仍在执行');
 const original = await browser.evaluate(() => window.turnRecovery.posts[0].request_id);
 await screen.getByRole('button', '新建对话').tap();
 await expect(screen.getByText('从一个问题开始')).toBeVisible();
 await screen.getByLabel('向知识库提问').fill('Draft from a later view');
 await browser.evaluate(() => window.turnRecovery.status('completed'));
 await screen.getByRole('button', '核对原请求').tap();
 await expect(screen.getByRole('button', 'Recovery scope · 第 1 轮')).toBeVisible();
 await expect(browser.locator('.messages')).toContainText('Confirmed original answer');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Draft from a later view');
 expect(await browser.evaluate(() => window.turnRecovery.reads)).toEqual([original, original]);
 expect(await browser.evaluate(() => window.turnRecovery.posts.length)).toBe(1);
 expect(await browser.locator('.message.assistant').count()).toBe(1);
});

test('a completed redacted recovery exposes no previous answer or new turn', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {lostResponse: true, redacted: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await browser.evaluate(() => window.turnRecovery.status('completed'));
 await screen.getByRole('button', '核对原请求').tap();
 await expect(screen.getByRole('button', 'Recovery scope · 第 1 轮')).toBeVisible();
 await expect(browser.locator('.messages')).toContainText('此历史答案的来源已不可访问');
 expect(await browser.evaluate(() => document.querySelector('.messages').textContent.includes('Confirmed original answer'))).toBe(false);
 expect(await browser.evaluate(() => window.turnRecovery.posts.length)).toBe(1);
});

test('a recovery response for a different request cannot unlock the original attempt', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {lostResponse: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await browser.evaluate(() => {window.turnRecovery.status('completed'); window.turnRecovery.mismatch(true);});
 await screen.getByRole('button', '核对原请求').tap();
 await expect(browser.locator('.request-recovery')).toContainText('查询结果与原请求身份不一致');
 expect(await browser.evaluate(() => ({posts: window.turnRecovery.posts.length, blocked: document.querySelector('[aria-label="发送问题"]').disabled}))).toEqual({posts: 1, blocked: true});
 expect(await browser.evaluate(() => document.querySelector('.messages').textContent.includes('Confirmed original answer'))).toBe(false);
});

test('the exact pre-handler recovery guard preserves the unadmitted question', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {rejected: true});
 await expect(screen.getByRole('alert')).toContainText('服务在接收本轮前已拒绝提交');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Keep the original question');
 expect(await browser.locator('.request-recovery').count()).toBe(0);
 expect(await browser.locator('.message').count()).toBe(0);
 expect(await browser.evaluate(() => ({posts: window.turnRecovery.posts.length, reads: window.turnRecovery.reads.length}))).toEqual({posts: 1, reads: 0});
});

test('model-sent false with an unresolved budget operation cannot unlock or discard its facts', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {commitUnknown: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await screen.getByText('服务恢复信息').tap();
 await expect(browser.locator('.request-recovery')).toContainText('COMMIT_UNKNOWN');
 await expect(browser.locator('.request-recovery')).toContainText('budget-operation');
 await expect(browser.locator('.request-recovery')).toContainText('inspect_operation');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Keep the original question');
 expect(await browser.evaluate(() => ({posts: window.turnRecovery.posts.length, blocked: document.querySelector('[aria-label="发送问题"]').disabled}))).toEqual({posts: 1, blocked: true});
});

test('a permanently interrupted request can be retained while a distinct blank conversation proceeds', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {lostResponse: true, permanentInterrupted: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await screen.getByRole('button', '核对原请求').tap();
 await expect(browser.locator('.request-recovery')).toContainText('原请求已中断');
 const original = await browser.evaluate(() => window.turnRecovery.posts[0].request_id);
 await screen.getByRole('button', '保留记录并新建对话').tap();
 await expect(screen.getByText('从一个问题开始')).toBeVisible();
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('');
 await expect(browser.locator('.retained-request')).toContainText(original);
 expect(await browser.evaluate(() => window.turnRecovery.posts.length)).toBe(1);
 await screen.getByLabel('向知识库提问').fill('A different question for a new session');
 await screen.getByRole('button', '发送问题').tap();
 await expect(browser.locator('.messages')).toContainText('Confirmed new-session answer');
 const sent = await browser.evaluate(() => window.turnRecovery.posts);
 expect(sent.length).toBe(2);
 expect(sent[1].message).toBe('A different question for a new session');
 expect(sent[1].session_id).toBe('distinct-session-2');
 expect(sent[1].request_id === original).toBe(false);
 await screen.getByLabel('向知识库提问').fill('Draft after distinct completion');
 await screen.getByRole('button', '核对保留请求').tap();
 await expect(browser.locator('.retained-request')).toContainText('原请求已中断');
 await expect(browser.locator('.messages')).toContainText('Confirmed new-session answer');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Draft after distinct completion');
 expect(await browser.evaluate(() => ({reads: window.turnRecovery.reads, sessions: window.turnRecovery.readSessions, posts: window.turnRecovery.posts.length, blocked: document.querySelector('[aria-label="发送问题"]').disabled}))).toEqual({reads: [original, original], sessions: ['recover-session', 'recover-session'], posts: 2, blocked: false});
 await screen.getByRole('button', 'Recovery scope · 第 0 轮').tap();
 await expect(browser.locator('.messages')).toContainText('Keep the original question');
 expect(await browser.evaluate(() => document.querySelector('[aria-label="发送问题"]').disabled)).toBe(true);
 expect(await browser.evaluate(() => window.turnRecovery.posts.length)).toBe(2);
});

test('a retained result updates only its card until explicitly opened and preserves the newer draft', async ({app, screen, browser}) => {
 await recoveryFixture(app, screen, browser, {lostResponse: true});
 await expect(browser.locator('.request-recovery')).toContainText('原请求结果待核对');
 await browser.evaluate(() => window.turnRecovery.status('not_found'));
 await screen.getByRole('button', '核对原请求').tap();
 await expect(browser.locator('.request-recovery')).toContainText('尚未查到原请求记录');
 await screen.getByRole('button', '保留记录并新建对话').tap();
 await screen.getByLabel('向知识库提问').fill('An independent conversation');
 await screen.getByRole('button', '发送问题').tap();
 await expect(browser.locator('.messages')).toContainText('Confirmed new-session answer');
 await screen.getByLabel('向知识库提问').fill('Preserve this newer draft');
 await browser.evaluate(() => window.turnRecovery.status('completed'));
 await screen.getByRole('button', '核对保留请求').tap();
 await expect(browser.locator('.retained-request')).toContainText('已找回原请求结果');
 await expect(browser.locator('.messages')).toContainText('Confirmed new-session answer');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Preserve this newer draft');
 await screen.getByRole('button', '打开已确认会话').tap();
 await expect(browser.locator('.retained-request')).toContainText('原请求已恢复');
 await expect(browser.locator('.messages')).toContainText('Confirmed original answer');
 await expect(screen.getByLabel('向知识库提问')).toHaveValue('Preserve this newer draft');
 expect(await browser.evaluate(() => ({posts: window.turnRecovery.posts.length, blocked: document.querySelector('[aria-label="发送问题"]').disabled, originalAnswers: document.querySelectorAll('.message.assistant').length}))).toEqual({posts: 2, blocked: false, originalAnswers: 1});
});

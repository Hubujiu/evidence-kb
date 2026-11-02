import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

async function fixture(app, screen, browser, createSession = true) {
  await browser.addInitScript(() => {
    const pending = new Map(), waiting = new Map(), turns = [];
    let holdCreation = false;
    const policy = {mode: 'AUTO', source_ids: [], versions: {}, version_policy: 'latest', lifetime: 'session'};
    const session = revision => ({session_id: 'session', scope_id: 'scope', revision,
      state: {source_policy: policy, constraints: []}});
    const ready = value => Promise.resolve({ok: true, status: 200, json: async () => value});
    const deferred = key => {
      const request = {};
      request.body = new Promise(resolve => { request.resolve = resolve; });
      request.reading = new Promise(resolve => { request.read = resolve; });
      pending.set(key, request);
      waiting.get(key)?.();
      return Promise.resolve({ok: true, status: 200, json: () => { request.read(); return request.body; }});
    };
    window.fetch = (path, options = {}) => {
      if (path === '/scopes') return ready([{scope_id: 'scope', name: 'Synthetic scope'}]);
      if (path === '/agent/capabilities') return ready({recommended: 'recommended',
        configurations: {recommended: {compact: true}}});
      if (path === '/scopes/scope/agent-sources') return ready([]);
      if (path === '/agent/sessions?limit=50') return ready({sessions: []});
      if (path === '/agent/runtime-status') return ready({configured: true, status: 'BLOCKED',
        usage_available: false, pending_or_unknown: null,
        remaining: {model: null, remote_embedding_calls: null}, cumulative_effective_usage_or_reserved: null});
      if (path === '/scopes/scope/sessions') return holdCreation ? deferred('creation') : ready(session(0));
      if (path === '/agent/sessions/session/turns') {
        turns.push(JSON.parse(options.body));
        return deferred('turn');
      }
      if (path === '/agent/sessions/session') return deferred('session');
      throw Error('Unexpected draft fixture request: ' + path);
    };
    window.__draftFixture = {
      turns,
      deferCreation: () => { holdCreation = true; },
      finishCreation: () => pending.get('creation').resolve(session(0)),
      wait: key => pending.has(key) ? pending.get(key).reading :
        new Promise(resolve => waiting.set(key, resolve)).then(() => pending.get(key).reading),
      finishTurn: () => pending.get('turn').resolve({answer: 'Synthetic answer', claims: [], evidence: [],
        status: 'answered', stop_reason: 'answered', trace: {}}),
      finishSession: () => pending.get('session').resolve(session(1)),
      flush: () => new Promise(requestAnimationFrame),
    };
  });
  await app.open('/agent');
  await expect(screen.getByRole('status')).toContainText('已连接，可直接提问。');
  await browser.locator('details:has(#runtimeStatus) > summary').tap();
  await expect(browser.locator('#runtimeStatus')).toContainText('预算账本暂不可读取');
  await expect(browser.locator('#runtimeStatus')).toContainText('模型 未知 次');
  if (createSession) {
    await screen.getByRole('button', '新建会话').tap();
    await expect(screen.getByRole('status')).toContainText('新会话已创建');
  }
}

async function send(screen, browser) {
  await screen.getByLabel('你的问题').fill('Submitted question');
  await screen.getByRole('button', '发送').tap();
  await browser.evaluate(() => window.__draftFixture.wait('turn'));
  expect(await browser.evaluate(() => window.__draftFixture.turns.map(turn => turn.message)))
    .toEqual(['Submitted question']);
}

async function finishTurn(browser) {
  await browser.evaluate(() => window.__draftFixture.finishTurn());
  await browser.evaluate(() => window.__draftFixture.wait('session'));
}

async function finishSession(screen, browser) {
  await browser.evaluate(() => window.__draftFixture.finishSession());
  await expect(screen.getByRole('status')).toContainText('本轮已结束，可以继续追问。');
  await expect(browser.locator('#transcript')).toContainText('Synthetic answer');
}

test('new draft typed during a pending turn survives its successful completion', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await send(screen, browser);
  await screen.getByLabel('你的问题').fill('Next draft while the turn is pending');
  await finishTurn(browser);
  await finishSession(screen, browser);
  expect(await browser.evaluate(() => document.querySelector('#question').value))
    .toBe('Next draft while the turn is pending');
});

test('identical text retyped after submission is a new draft, not the submitted owner', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await send(screen, browser);
  await screen.getByLabel('你的问题').fill('');
  await screen.getByLabel('你的问题').fill('Submitted question');
  await finishTurn(browser);
  await finishSession(screen, browser);
  expect(await browser.evaluate(() => document.querySelector('#question').value)).toBe('Submitted question');
});

test('new draft typed during the session refresh survives the older turn', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await send(screen, browser);
  await finishTurn(browser);
  await screen.getByLabel('你的问题').fill('Next draft while the session refresh is pending');
  await finishSession(screen, browser);
  expect(await browser.evaluate(() => document.querySelector('#question').value))
    .toBe('Next draft while the session refresh is pending');
});

test('new draft typed during implicit session creation is not owned by the first turn', async ({app, screen, browser}) => {
  await fixture(app, screen, browser, false);
  await browser.evaluate(() => window.__draftFixture.deferCreation());
  await screen.getByLabel('你的问题').fill('Submitted question');
  await screen.getByRole('button', '发送').tap();
  await browser.evaluate(() => window.__draftFixture.wait('creation'));
  await screen.getByLabel('你的问题').fill('Next draft while the initial session is pending');
  await browser.evaluate(() => window.__draftFixture.finishCreation());
  await browser.evaluate(() => window.__draftFixture.wait('turn'));
  expect(await browser.evaluate(() => window.__draftFixture.turns.map(turn => turn.message)))
    .toEqual(['Submitted question']);
  await finishTurn(browser);
  await finishSession(screen, browser);
  expect(await browser.evaluate(() => document.querySelector('#question').value))
    .toBe('Next draft while the initial session is pending');
});

test('unchanged submitted draft clears while unavailable budget stays explicitly unknown', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await send(screen, browser);
  await finishTurn(browser);
  await finishSession(screen, browser);
  expect(await browser.evaluate(() => document.querySelector('#question').value)).toBe('');
  await expect(browser.locator('#runtimeStatus')).toContainText('预算账本暂不可读取');
  expect(await browser.evaluate(() => window.__draftFixture.turns.length)).toBe(1);
});

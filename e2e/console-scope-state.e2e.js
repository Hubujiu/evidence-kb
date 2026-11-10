import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

async function fixture(app, screen, browser) {
  await browser.addInitScript(() => {
    const pending = new Map(), waiting = new Map(), commits = [], requests = [];
    let controlled = false;
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
      requests.push({path, method: options.method || 'GET', scope: document.querySelector('#kb')?.value});
      if (path === '/identity') return ready({tenant_id: 'synthetic', principal_id: 'reader'});
      if (path === '/knowledge-bases' && options.method === 'POST') return deferred('create');
      if (path === '/knowledge-bases') return controlled ? deferred('list') : ready([{kb_id: 'scope', name: 'Old scope'}]);
      if (path === '/knowledge-bases/scope/sources') return deferred('upload');
      if (path === '/knowledge-bases/scope/search') return deferred('search');
      throw Error('Unexpected scope fixture request: ' + path);
    };
    window.__scopeFixture = {
      commits, requests,
      arm: () => {
        controlled = true;
        const target = document.querySelector('#ingest');
        const text = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
        Object.defineProperty(target, 'textContent', {
          get() { return text.get.call(this); },
          set(value) {
            commits.push({scope: document.querySelector('#kb').value, text: value});
            text.set.call(this, value);
          },
        });
      },
      wait: key => pending.has(key) ? pending.get(key).reading :
        new Promise(resolve => waiting.set(key, resolve)).then(() => pending.get(key).reading),
      resolve: (key, value) => pending.get(key).resolve(value),
      flush: () => new Promise(requestAnimationFrame),
    };
  });
  await app.open('/console');
  await screen.getByLabel('本地访问令牌').fill('synthetic-controlled-scope-token');
  await screen.getByRole('button', '连接').tap();
  await expect(screen.getByRole('status')).toContainText('身份已验证。');
  expect(await browser.evaluate(() => document.querySelector('#kb').value)).toBe('scope');
  await browser.evaluate(() => window.__scopeFixture.arm());
}

async function startUpload(screen, browser) {
  await browser.locator('#file').setInputFiles('fixtures/source.md');
  await screen.getByRole('button', '上传并解析').tap();
  await browser.evaluate(() => window.__scopeFixture.wait('upload'));
}

async function startCreate(screen, browser) {
  await screen.getByLabel('新知识库名称').fill('New scope');
  await screen.getByRole('button', '新建知识库').tap();
  await browser.evaluate(() => window.__scopeFixture.wait('create'));
  await browser.evaluate(() => window.__scopeFixture.resolve('create', {kb_id: 'next'}));
  await browser.evaluate(() => window.__scopeFixture.wait('list'));
}

test('concurrent create cannot publish an old upload result under the newly selected scope', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await startUpload(screen, browser);
  await startCreate(screen, browser);
  await browser.evaluate(() => {
    const fixture = window.__scopeFixture;
    fixture.resolve('list', [{kb_id: 'scope', name: 'Old scope'}, {kb_id: 'next', name: 'New scope'}]);
    fixture.resolve('upload', {document: {status: 'READY', elements: [{}]}});
    return fixture.flush();
  });
  await expect(screen.getByRole('status')).toContainText('已创建知识库。');
  expect(await browser.evaluate(() => document.querySelector('#kb').value)).toBe('next');
  const observed = await browser.evaluate(() => ({commits: window.__scopeFixture.commits,
    request: window.__scopeFixture.requests.find(item => item.path.endsWith('/sources'))}));
  expect(observed.request.scope).toBe('scope');
  expect(observed.commits.filter(item => item.text.includes('READY') && item.scope !== 'scope')).toEqual([]);
});

test('current upload still publishes its actual DOM result in the owning scope', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await startUpload(screen, browser);
  await browser.evaluate(() => {
    window.__scopeFixture.resolve('upload', {document: {status: 'READY', elements: [{}]}});
    return window.__scopeFixture.flush();
  });
  await expect(browser.locator('#ingest')).toContainText('source.md · READY · 1 个结构元素');
  expect(await browser.evaluate(() => window.__scopeFixture.commits))
    .toEqual([{scope: 'scope', text: 'source.md · READY · 1 个结构元素'}]);
});

test('concurrent create retains its later clear after an older search completion', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await screen.getByLabel('检索问题或关键词').fill('original');
  await screen.getByRole('button', '在当前库检索').tap();
  await browser.evaluate(() => window.__scopeFixture.wait('search'));
  await startCreate(screen, browser);
  await browser.evaluate(() => {
    const fixture = window.__scopeFixture;
    fixture.resolve('list', [{kb_id: 'scope', name: 'Old scope'}, {kb_id: 'next', name: 'New scope'}]);
    fixture.resolve('search', {hits: [{text: 'Old scope original', citation_id: 'old'}]});
    return fixture.flush();
  });
  await expect(screen.getByRole('status')).toContainText('已创建知识库。');
  expect(await browser.evaluate(() => ({scope: document.querySelector('#kb').value,
    result: document.querySelector('#results').textContent}))).toEqual({scope: 'next', result: ''});
});

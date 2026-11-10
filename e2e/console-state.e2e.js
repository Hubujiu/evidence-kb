import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

async function fixture(app, screen, browser) {
  await browser.addInitScript(() => {
    const pending = new Map(), waiting = new Map(), blobs = new WeakMap();
    const created = [], revoked = [];
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = blob => {
      const url = create(blob);
      created.push({url, owner: blobs.get(blob)});
      return url;
    };
    URL.revokeObjectURL = url => { revoked.push(url); revoke(url); };
    const ready = data => Promise.resolve({ok: true, status: 200, json: async () => data});
    window.fetch = path => {
      if (path === '/identity') return ready({tenant_id: 'synthetic', principal_id: 'reader'});
      if (path === '/knowledge-bases') return ready([{kb_id: 'scope', name: 'Synthetic scope'}]);
      if (path === '/knowledge-bases/scope/search') return ready({hits: [
        {text: 'Original A', citation_id: 'A'}, {text: 'Original B', citation_id: 'B'},
      ]});
      if (!/^\/knowledge-bases\/scope\/citations\/[AB](\/preview)?$/.test(path)) {
        throw Error('Unexpected fixture request: ' + path);
      }
      const request = {};
      request.body = new Promise((resolve, reject) => { request.resolveBody = resolve; request.rejectBody = reject; });
      request.body.catch(() => {});
      request.bodyRead = new Promise(resolve => { request.read = resolve; });
      request.response = new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
      pending.set(path, request);
      waiting.get(path)?.();
      return request.response;
    };
    const get = path => {
      const value = pending.get(path);
      if (!value) throw Error('Request was not started: ' + path);
      return value;
    };
    const headers = (path, status = 200) => get(path).resolve({ok: status === 200, status,
      json: () => { get(path).read(); return get(path).body; },
      blob: () => { get(path).read(); return get(path).body; }});
    const body = (path, value) => {
      if (path.endsWith('/preview')) {
        const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII='), x => x.charCodeAt(0));
        const blob = new Blob([bytes], {type: 'image/png'});
        blobs.set(blob, value);
        get(path).resolveBody(blob);
      } else get(path).resolveBody(value);
    };
    window.__consoleFixture = {
      headers, body, created, revoked,
      wait: path => pending.has(path) ? Promise.resolve() : new Promise(resolve => waiting.set(path, resolve)),
      waitBody: path => get(path).bodyRead,
      respond: (path, value) => { headers(path); body(path, value); },
      fail: (path, phase) => get(path)[phase === 'body' ? 'rejectBody' : 'reject'](Error('synthetic stale failure')),
      flush: () => new Promise(requestAnimationFrame),
    };
  });
  await app.open('/console');
  await screen.getByLabel('本地访问令牌').fill('synthetic-token-for-controlled-browser-only');
  await screen.getByRole('button', '连接').tap();
  await expect(screen.getByRole('status')).toContainText('身份已验证。');
  await screen.getByLabel('检索问题或关键词').fill('original');
  await screen.getByRole('button', '在当前库检索').tap();
  await expect(browser.locator('#results')).toContainText('Original B');
}

const path = (id, preview = false) => `/knowledge-bases/scope/citations/${id}${preview ? '/preview' : ''}`;
const metadata = id => ({filename: `${id}.pdf`, source_version_id: `version-${id}`,
  text: `Original ${id}`, anchors: {page: id}, media_type: 'application/pdf'});

async function select(browser, id) {
  await browser.evaluate(id => document.querySelectorAll('#results button')[id === 'A' ? 0 : 1].click(), id);
  await browser.evaluate(value => window.__consoleFixture.wait(value), path(id));
}

async function deliverMetadata(browser, id) {
  await browser.evaluate(({url, value}) => window.__consoleFixture.respond(url, value),
    {url: path(id), value: metadata(id)});
  await browser.evaluate(value => window.__consoleFixture.wait(value), path(id, true));
}

async function deliverPreview(browser, id) {
  await browser.evaluate(({url, value}) => window.__consoleFixture.respond(url, value),
    {url: path(id, true), value: id});
  await browser.evaluate(() => window.__consoleFixture.flush());
}

async function view(browser) {
  return browser.evaluate(() => {
    const fixture = window.__consoleFixture;
    return {title: document.querySelector('#citation h3')?.textContent || null,
      previews: [...document.querySelectorAll('#citation img')].map(image => {
        const resource = fixture.created.find(item => item.url === image.src);
        return {owner: resource?.owner, revoked: fixture.revoked.includes(image.src)};
      }), downloads: document.querySelectorAll('#citation button').length,
      message: document.querySelector('#message').textContent};
  });
}

test('newer citation survives older metadata completion', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await select(browser, 'A');
  await select(browser, 'B');
  await deliverMetadata(browser, 'B');
  await deliverPreview(browser, 'B');
  await browser.evaluate(({url, value}) => window.__consoleFixture.respond(url, value),
    {url: path('A'), value: metadata('A')});
  await browser.evaluate(() => window.__consoleFixture.flush());
  expect(await view(browser)).toEqual({title: 'B.pdf', previews: [{owner: 'B', revoked: false}],
    downloads: 1, message: ''});
});

test('late preview body cannot mix citations or revoke the current URL', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await select(browser, 'A');
  await deliverMetadata(browser, 'A');
  await browser.evaluate(value => window.__consoleFixture.headers(value), path('A', true));
  await browser.evaluate(value => window.__consoleFixture.waitBody(value), path('A', true));
  await select(browser, 'B');
  await deliverMetadata(browser, 'B');
  await deliverPreview(browser, 'B');
  await browser.evaluate(value => window.__consoleFixture.body(value, 'A'), path('A', true));
  await browser.evaluate(() => window.__consoleFixture.flush());
  expect(await view(browser)).toEqual({title: 'B.pdf', previews: [{owner: 'B', revoked: false}],
    downloads: 1, message: ''});
});

test('stale citation fetch failure does not replace the newer view status', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await select(browser, 'A');
  await select(browser, 'B');
  await deliverMetadata(browser, 'B');
  await deliverPreview(browser, 'B');
  await browser.evaluate(value => window.__consoleFixture.fail(value, 'fetch'), path('A'));
  await browser.evaluate(() => window.__consoleFixture.flush());
  expect((await view(browser)).message).toBe('');
});

test('scope switch rejects late error-body status and successful preview body', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await select(browser, 'A');
  await browser.evaluate(value => window.__consoleFixture.headers(value, 503), path('A'));
  await browser.evaluate(value => window.__consoleFixture.waitBody(value), path('A'));
  await select(browser, 'B');
  await deliverMetadata(browser, 'B');
  await browser.evaluate(value => window.__consoleFixture.headers(value), path('B', true));
  await browser.evaluate(value => window.__consoleFixture.waitBody(value), path('B', true));
  await screen.getByLabel('本地访问令牌').fill('synthetic-new-token-for-controlled-browser-only');
  await screen.getByRole('button', '连接').tap();
  await expect(screen.getByRole('status')).toContainText('身份已验证。');
  await browser.evaluate(({a, b}) => {
    window.__consoleFixture.body(a, {detail: 'stale server detail'});
    window.__consoleFixture.body(b, 'B');
    return window.__consoleFixture.flush();
  }, {a: path('A'), b: path('B', true)});
  expect(await browser.evaluate(() => document.querySelector('#citation').textContent)).toBe('');
  expect((await view(browser)).message).toBe('身份已验证。');
});

test('current errors remain visible and scope reset releases its preview URL once', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await select(browser, 'A');
  await browser.evaluate(value => window.__consoleFixture.fail(value, 'fetch'), path('A'));
  await expect(screen.getByRole('status')).toContainText('synthetic stale failure');
  await select(browser, 'B');
  await deliverMetadata(browser, 'B');
  await deliverPreview(browser, 'B');
  await screen.getByLabel('本地访问令牌').fill('synthetic-new-token-for-controlled-browser-only');
  expect(await browser.evaluate(() => ({images: document.querySelectorAll('#citation img').length,
    created: window.__consoleFixture.created.length, revoked: window.__consoleFixture.revoked.length})))
    .toEqual({images: 0, created: 1, revoked: 1});
});


test('current search failure remains visible after its intentional view reset', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await browser.evaluate(() => { window.fetch = () => Promise.reject(Error('synthetic current search failure')); });
  await screen.getByRole('button', '在当前库检索').tap();
  await expect(screen.getByRole('status')).toContainText('synthetic current search failure');
});


test('queued citation failure rechecks ownership at the final status write', async ({app, screen, browser}) => {
  await fixture(app, screen, browser);
  await select(browser, 'A');
  await browser.evaluate(value => {
    window.__consoleFixture.fail(value, 'fetch');
    queueMicrotask(() => queueMicrotask(() => queueMicrotask(() => {
      document.querySelectorAll('#results button')[1].click();
    })));
    return window.__consoleFixture.wait('/knowledge-bases/scope/citations/B');
  }, path('A'));
  await browser.evaluate(() => window.__consoleFixture.flush());
  expect((await view(browser)).message).toBe('');
  await deliverMetadata(browser, 'B');
  await deliverPreview(browser, 'B');
  expect(await view(browser)).toEqual({title: 'B.pdf', previews: [{owner: 'B', revoked: false}],
    downloads: 1, message: ''});
});

import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

test('offline knowledge workspace from document to citation', async ({app, screen, browser}) => {
 await app.open('/');
 await screen.getByRole('button', '知识库管理').tap();
 await browser.locator('.connect-banner button').tap();
 await screen.getByLabel('访问令牌').fill('synthetic-web-token-for-offline-browser-acceptance');
 await screen.getByRole('button', '连接知识库').tap();
 await expect(screen.getByRole('button', '切换连接')).toBeVisible();
 await screen.getByLabel('新知识库名称').fill('离线流程验证');
 await screen.getByRole('button', '创建').tap();
 await expect(screen.getByRole('status')).toContainText('知识库已创建');
 await browser.locator('#work-file').setInputFiles('fixtures/source.md');
 await screen.getByRole('button', '上传并解析').tap();
 await expect(screen.getByRole('status')).toContainText('READY');
 await screen.getByRole('button', '构建并发布索引').tap();
 await expect(screen.getByRole('status')).toContainText('索引已发布');
 await screen.getByLabel('检索问题或关键词').fill('QUARTZMARKER');
 await screen.getByRole('button', '检索').tap();
 await expect(browser.locator('.search-results')).toContainText('owner review before publication');
 await screen.getByRole('button', '查看原文与定位').tap();
 await expect(screen.getByRole('dialog')).toContainText('source.md');
 await expect(screen.getByRole('dialog')).toContainText('owner review before publication');
 await expect(browser.frameLocator('iframe[title="原文内容"]').locator('body')).toContainText('owner review before publication');
 await screen.getByRole('button', '关闭').tap();
 await screen.getByRole('button', '进入对话').tap();
 await screen.getByLabel('向知识库提问').fill('这个流程是什么？');
 await screen.getByRole('button', '发送问题').tap();
 await expect(screen.getByRole('alert')).toContainText('对话服务尚未配置');
});

test('late workbench create cannot publish scopes across an identity switch', async ({app, screen, browser}) => {
 await app.open('/');
 await browser.evaluate(() => {
  let reads = 0;
  let release;
  let started;
  const waiting = new Promise(resolve => { started = resolve; });
  let staleSignal;
  const ready = value => Promise.resolve({ok: true, status: 200, json: async () => value});
  window.fetch = (path, options = {}) => {
   const token = options.headers.Authorization;
   if (path === '/capabilities') return ready({agent_conversations: null});
   if (path === '/scopes' && options.method === 'POST') return ready({scope_id: 'created-a'});
   if (path === '/scopes' && token === 'Bearer identity-a') {
    reads++;
    if (reads > 1) {
     staleSignal = options.signal;
     return Promise.resolve({ok: true, status: 200, json: () => {
      const body = new Promise(resolve => { release = resolve; });
      started();
      return body;
     }});
    }
    return ready([{scope_id: 'scope-a', name: 'Identity A'}]);
   }
   if (path === '/scopes' && token === 'Bearer identity-b') return ready([{scope_id: 'scope-b', name: 'Identity B'}]);
   throw Error('Unexpected synthetic request');
  };
  window.workbenchRace = {
   wait: () => waiting,
   release: () => release([{scope_id: 'created-a', name: 'Stale A'}]),
   aborted: () => staleSignal?.aborted === true,
   flush: () => new Promise(requestAnimationFrame),
  };
 });
 await screen.getByRole('button', '知识库管理').tap();
 await browser.locator('.connect-banner button').tap();
 await screen.getByLabel('访问令牌').fill('identity-a');
 await screen.getByRole('button', '连接知识库').tap();
 await screen.getByLabel('新知识库名称').fill('Pending A');
 await screen.getByRole('button', '创建').tap();
 await browser.evaluate(() => window.workbenchRace.wait());
 await screen.getByRole('button', '切换连接').tap();
 await screen.getByLabel('访问令牌').fill('identity-b');
 await screen.getByRole('button', '连接知识库').tap();
 await expect(browser.locator('#work-scope')).toHaveValue('scope-b');
 await browser.evaluate(() => window.workbenchRace.release());
 await browser.evaluate(() => window.workbenchRace.flush());
 expect(await browser.evaluate(() => window.workbenchRace.aborted())).toBe(true);
 await expect(browser.locator('#work-scope')).toHaveValue('scope-b');
 expect(await browser.evaluate(() => Array.from(document.querySelectorAll('#work-scope option'), option => option.textContent))).toEqual(['Identity B']);
});

test('late preview body cannot allocate a blob after leaving the workbench', async ({app, screen, browser}) => {
 await app.open('/');
 await browser.evaluate(() => {
  let release;
  let started;
  const waiting = new Promise(resolve => { started = resolve; });
  let created = 0;
  let revoked = 0;
  let hold = true;
  const pendingTimers = new Set();
  let timerIdentity = -1;
  const schedule = window.setTimeout.bind(window);
  const cancelTimer = window.clearTimeout.bind(window);
  window.setTimeout = (callback, delay, ...args) => {
   if (delay !== 1000) return schedule(callback, delay, ...args);
   const timer = timerIdentity--;
   pendingTimers.add(timer);
   return timer;
  };
  window.clearTimeout = timer => {
   if (!pendingTimers.delete(timer)) cancelTimer(timer);
  };
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = blob => { created++; return create(blob); };
  URL.revokeObjectURL = url => { revoked++; revoke(url); };
  const ready = value => Promise.resolve({ok: true, status: 200, json: async () => value});
  window.fetch = path => {
   if (path === '/scopes') return ready([{scope_id: 'scope-a', name: 'Identity A'}]);
   if (path === '/capabilities') return ready({agent_conversations: null});
   if (path.endsWith('/search')) return ready({hits: [{citation_id: 'c1', text: 'Evidence marker'}]});
   if (path.endsWith('/citations/c1')) return ready({filename: 'source.md', source_version_id: 'v1', text: 'Evidence marker', anchors: []});
   if (path.endsWith('/original')) return Promise.resolve({ok: true, status: 200,
    blob: async () => new Blob(['original'], {type: 'text/plain'})});
   if (path.endsWith('/preview')) return Promise.resolve({ok: true, status: 200, blob: () => {
    if (!hold) return Promise.resolve(new Blob(['preview'], {type: 'text/plain'}));
    const body = new Promise(resolve => { release = resolve; });
    started(); return body;
   }});
   throw Error('Unexpected synthetic request');
  };
  window.workbenchRace = {
   wait: () => waiting,
   release: () => { hold = false; release(new Blob(['late preview'], {type: 'text/plain'})); },
   counts: () => ({created, revoked}),
   pendingDownloads: () => pendingTimers.size,
   flush: () => new Promise(requestAnimationFrame),
  };
 });
 await screen.getByRole('button', '知识库管理').tap();
 await browser.locator('.connect-banner button').tap();
 await screen.getByLabel('访问令牌').fill('identity-a');
 await screen.getByRole('button', '连接知识库').tap();
 await screen.getByLabel('检索问题或关键词').fill('marker');
 await screen.getByRole('button', '检索').tap();
 await screen.getByRole('button', '查看原文与定位').tap();
 await browser.evaluate(() => window.workbenchRace.wait());
 await screen.getByRole('button', '知识库对话').tap();
 await browser.evaluate(() => window.workbenchRace.release());
 await browser.evaluate(() => window.workbenchRace.flush());
 expect(await browser.evaluate(() => window.workbenchRace.counts())).toEqual({created: 0, revoked: 0});
 await screen.getByRole('button', '知识库管理').tap();
 await screen.getByLabel('检索问题或关键词').fill('marker');
 await screen.getByRole('button', '检索').tap();
 await screen.getByRole('button', '查看原文与定位').tap();
 await expect(screen.getByRole('dialog')).toContainText('source.md');
 expect(await browser.evaluate(() => window.workbenchRace.counts())).toEqual({created: 1, revoked: 0});
 const download = await browser.waitForDownload(() => screen.getByRole('button', '下载原件').tap());
 expect(download.suggestedFilename).toBe('source.md');
 expect(await browser.evaluate(() => window.workbenchRace.pendingDownloads())).toBe(1);
 await screen.getByRole('button', '关闭').tap();
 expect(await browser.evaluate(() => window.workbenchRace.counts())).toEqual({created: 2, revoked: 1});
 await screen.getByRole('button', '知识库对话').tap();
 await browser.evaluate(() => window.workbenchRace.flush());
 expect(await browser.evaluate(() => window.workbenchRace.counts())).toEqual({created: 2, revoked: 2});
 expect(await browser.evaluate(() => window.workbenchRace.pendingDownloads())).toBe(0);
});

import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

async function connect(app, screen, browser, token, first = false) {
  if (first) await app.open('/');
  if (first) await browser.locator('.top-connect').tap();
  else await screen.getByRole('button', '切换连接').tap();
  await screen.getByLabel('访问令牌').fill(token);
  await screen.getByRole('button', '连接知识库').tap();
  await expect(screen.getByRole('button', '切换连接')).toBeVisible();
  if (first) await screen.getByRole('button', '知识库管理').tap();
}

async function openPublishedCitation(screen, browser) {
  await browser.locator('#work-file').setInputFiles('fixtures/source.md');
  await screen.getByRole('button', '上传并解析').tap();
  await expect(screen.getByRole('status')).toContainText('READY');
  await screen.getByRole('button', '构建并发布索引').tap();
  await expect(screen.getByRole('status')).toContainText('索引已发布。');
  await screen.getByLabel('检索问题或关键词').fill('QUARTZMARKER');
  await screen.getByRole('button', '检索').tap();
  await expect(browser.locator('.search-results')).toContainText('owner review before publication');
  await screen.getByRole('button', '查看原文与定位').tap();
  const original = await readFile(new URL('./fixtures/source.md', import.meta.url));
  const hash = createHash('sha256').update(original).digest('hex');
  await expect(screen.getByRole('dialog')).toContainText(hash);
  return hash;
}

test('source upload, BM25 publication, citation and original download', async ({app, screen, browser}) => {
  await connect(app, screen, browser, process.env.KB_E2E_TOKEN, true);
  await screen.getByLabel('新知识库名称').fill('Browser source acceptance');
  await screen.getByRole('button', '创建').tap();
  await expect(screen.getByRole('status')).toContainText('知识库已创建');
  const hash = await openPublishedCitation(screen, browser);
  const download = await browser.waitForDownload(() => screen.getByRole('button', '下载原件').tap());
  expect(download.suggestedFilename).toBe('source.md');
  expect(createHash('sha256').update(await readFile(download.absolutePath)).digest('hex')).toBe(hash);
});

test('switching tenants clears and isolates knowledge bases', async ({app, screen, browser}) => {
  await connect(app, screen, browser, process.env.KB_E2E_TOKEN, true);
  await screen.getByLabel('新知识库名称').fill('Tenant private evidence');
  await screen.getByRole('button', '创建').tap();
  await expect(screen.getByRole('status')).toContainText('知识库已创建');
  await expect(screen.getByLabel('当前知识库')).toContainText('Tenant private evidence');
  await openPublishedCitation(screen, browser);
  await screen.getByRole('button', '关闭').tap();
  const oldScope = await browser.evaluate(async token => {
    const prefix = '/knowledge-bases/' + encodeURIComponent(document.querySelector('#work-scope').value);
    const response = await fetch(prefix + '/search', {
      method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token},
      body: JSON.stringify({query: 'QUARTZMARKER', channel: 'bm25'}),
    });
    if (!response.ok) throw new Error('Owner search must succeed before tenant switch');
    const {hits} = await response.json();
    return {prefix, citation: prefix + '/citations/' + encodeURIComponent(hits[0].citation_id)};
  }, process.env.KB_E2E_TOKEN);
  await connect(app, screen, browser, process.env.KB_E2E_OTHER_TOKEN);
  expect(await browser.locator('#work-scope option').count()).toBe(1);
  await expect(browser.locator('#work-scope')).toContainText('暂无知识库');
  await expect(browser.locator('.search-results')).not.toContainText('owner review');
  const denied = await browser.evaluate(async ({prefix, citation, token}) => {
    const headers = {Authorization: 'Bearer ' + token};
    const requests = [
      [prefix + '/search', {method: 'POST', headers: {...headers, 'Content-Type': 'application/json'},
        body: JSON.stringify({query: 'QUARTZMARKER', channel: 'bm25'})}],
      [citation, {headers}], [citation + '/original', {headers}],
    ];
    return Promise.all(requests.map(async ([url, options]) => (await fetch(url, options)).status));
  }, {...oldScope, token: process.env.KB_E2E_OTHER_TOKEN});
  expect(denied).toEqual([403, 403, 403]);
});

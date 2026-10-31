import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

async function connect(app, screen, token) {
  await app.open('/');
  await screen.getByLabel('本地访问令牌').fill(token);
  await screen.getByRole('button', '连接').tap();
  await expect(screen.getByRole('status')).toContainText('身份已验证。');
}

test('source upload, BM25 publication, citation and original download', async ({app, screen, browser}) => {
  await connect(app, screen, process.env.KB_E2E_TOKEN);
  await screen.getByLabel('新知识库名称').fill('Browser source acceptance');
  await screen.getByRole('button', '新建知识库').tap();
  await expect(screen.getByRole('status')).toContainText('已创建知识库。');
  await browser.locator('#file').setInputFiles('fixtures/source.md');
  await screen.getByRole('button', '上传并解析').tap();
  await expect(browser.locator('#ingest')).toContainText('READY');
  await screen.getByRole('button', '构建并发布索引').tap();
  await expect(screen.getByRole('status')).toContainText('索引已发布。');
  await screen.getByLabel('检索问题或关键词').fill('QUARTZMARKER');
  await screen.getByRole('button', '在当前库检索').tap();
  await expect(browser.locator('#results')).toContainText('owner review before publication');
  await screen.getByRole('button', '查看原文与定位').tap();
  const original = await readFile(new URL('./fixtures/source.md', import.meta.url));
  const hash = createHash('sha256').update(original).digest('hex');
  await expect(browser.locator('#citation')).toContainText(hash);
  const download = await browser.waitForDownload(() => screen.getByRole('button', '下载原件').tap());
  expect(download.suggestedFilename).toBe('source.md');
  expect(createHash('sha256').update(await readFile(download.absolutePath)).digest('hex')).toBe(hash);
});

test('switching tenants clears and isolates knowledge bases', async ({app, screen, browser}) => {
  await connect(app, screen, process.env.KB_E2E_TOKEN);
  await screen.getByLabel('新知识库名称').fill('Tenant private evidence');
  await screen.getByRole('button', '新建知识库').tap();
  await expect(screen.getByRole('status')).toContainText('已创建知识库。');
  await expect(browser.locator('#kb')).toContainText('Tenant private evidence');
  await screen.getByLabel('本地访问令牌').fill(process.env.KB_E2E_OTHER_TOKEN);
  await screen.getByRole('button', '连接').tap();
  await expect(screen.getByRole('status')).toContainText('身份已验证。');
  expect(await browser.evaluate(() => document.querySelector('#kb').options.length)).toBe(0);
  expect(await browser.evaluate(() => document.querySelector('#citation').textContent)).toBe('');
});

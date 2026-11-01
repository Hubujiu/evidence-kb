import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

test('split storage browser upload, hybrid publication and dense citation', async ({app, screen, browser}) => {
  await app.open('/');
  await screen.getByLabel('本地访问令牌').fill(process.env.KB_E2E_TOKEN);
  await screen.getByRole('button', '连接').tap();
  await expect(screen.getByRole('status')).toContainText('身份已验证。');
  await screen.getByLabel('新知识库名称').fill('Browser split storage synthetic acceptance');
  await screen.getByRole('button', '新建知识库').tap();
  await expect(screen.getByRole('status')).toContainText('已创建知识库。');
  await browser.locator('#file').setInputFiles('fixtures/source.md');
  await screen.getByRole('button', '上传并解析').tap();
  await expect(browser.locator('#ingest')).toContainText('READY');
  const result = await browser.evaluate(async () => {
    const headers = {Authorization: 'Bearer ' + document.querySelector('#token').value,
      'Content-Type': 'application/json'};
    const prefix = '/knowledge-bases/' + encodeURIComponent(document.querySelector('#kb').value);
    const request = async (path, body) => {
      const response = await fetch(path, {headers, method: body ? 'POST' : 'GET',
        ...(body ? {body: JSON.stringify(body)} : {})});
      if (!response.ok) throw new Error(path + ': ' + response.status + ' ' + await response.text());
      return response.json();
    };
    const lexical = await request(prefix + '/builds', {channels: ['bm25']});
    await request(prefix + '/builds/' + lexical.build_id + '/publish', {});
    const source = (await request(prefix + '/search', {query: 'QUARTZMARKER', channel: 'bm25'})).hits[0];
    const policy = await fetch('/sources/' + source.source_id + '/policy', {
      method: 'PUT', headers, body: JSON.stringify({policy: {native_parse: true, embedding: true,
        llm_context: true, allowed_providers: ['storage_fixture']}}),
    });
    if (!policy.ok) throw new Error('Cannot authorize synthetic local embedding: ' + await policy.text());
    const hybrid = await request(prefix + '/builds', {channels: ['hybrid']});
    await request(prefix + '/builds/' + hybrid.build_id + '/publish', {});
    const channels = {};
    for (const channel of ['bm25', 'dense', 'hybrid']) {
      const {hits} = await request(prefix + '/search', {query: 'QUARTZMARKER', channel});
      channels[channel] = hits.map(hit => ({build: hit.build_id, text: hit.text}));
      if (!hits.length) throw new Error(channel + ' returned no authorized evidence');
    }
    const dense = (await request(prefix + '/search', {query: 'QUARTZMARKER', channel: 'dense'})).hits[0];
    const citation = await request(prefix + '/citations/' + dense.citation_id);
    return {channels, build: hybrid.build_id, citation: {version: citation.source_version_id,
      anchors: citation.anchors.length}};
  });
  for (const channel of ['bm25', 'dense', 'hybrid']) {
    expect(result.channels[channel].every(hit => hit.build === result.build)).toBe(true);
    expect(result.channels[channel].some(hit => hit.text.includes('owner review before publication'))).toBe(true);
  }
  expect(result.citation.anchors > 0).toBe(true);
  expect(result.citation.version.length).toBe(64);
  await screen.getByLabel('检索问题或关键词').fill('QUARTZMARKER');
  await screen.getByRole('button', '在当前库检索').tap();
  await expect(browser.locator('#results')).toContainText('owner review before publication');
  await screen.getByRole('button', '查看原文与定位').tap();
  await expect(browser.locator('#citation')).toContainText(result.citation.version);
});

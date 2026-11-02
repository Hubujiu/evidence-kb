import { web } from '@e2e-dev/web';

const endpoint = process.env.KB_E2E_CDP_ENDPOINT;
if (endpoint) {
  const url = new URL(endpoint);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password) {
    throw new Error('Controlled CDP endpoint must use an explicit loopback HTTP port');
  }
}

export default {
  tests: process.env.KB_E2E_TESTS || '*.e2e.js',
  workers: 1,
  retries: 0,
  cache: 'off',
  trace: 'on',
  reporters: ['list', 'junit'],
  targets: [{name: 'chromium', engine: web(endpoint ? {connect: {cdpEndpoint: () => endpoint}} : {}),
    app: {url: process.env.KB_E2E_URL}}],
};

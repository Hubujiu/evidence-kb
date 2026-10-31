import { web } from '@e2e-dev/web';

export default {
  tests: '*.e2e.js',
  workers: 1,
  retries: 0,
  cache: 'off',
  trace: 'on',
  reporters: ['list', 'junit'],
  targets: [{name: 'chromium', engine: web(), app: {url: process.env.KB_E2E_URL}}],
};

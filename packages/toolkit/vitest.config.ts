import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    silent: 'passed-only',
    // config.ts parses the environment at import time.
    env: {
      API_KEY: 'test-key',
      SCRAPLING_URL: 'http://scrapling.test:8000',
      CAMOUFOX_URL: 'http://camoufox.test:8000',
      SEARXNG_URL: 'http://searxng.test:8080',
    },
  },
});

import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  // Test against the toolkit's source, so `pnpm test` needs no build first.
  resolve: {
    alias: { '@web-tools/toolkit': fileURLToPath(new URL('../toolkit/src/index.ts', import.meta.url)) },
  },
  test: {
    include: ['test/**/*.test.ts'],
    silent: 'passed-only',
    env: {
      API_KEY: 'test-key',
      SCRAPLING_URL: 'http://scrapling.test:8000',
      CAMOUFOX_URL: 'http://camoufox.test:8000',
      SEARXNG_URL: 'http://searxng.test:8080',
    },
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      reportsDirectory: 'coverage',
      // This package's own code only; the toolkit is measured in its own package.
      include: ['src/**/*.ts'],
      exclude: ['src/**/*.d.ts'],
      // `pnpm test:coverage` (and CI) fails below this.
      thresholds: { lines: 80 },
    },
  },
});

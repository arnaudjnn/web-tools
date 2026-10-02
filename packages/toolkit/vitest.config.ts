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
      // The dedicated forms service: every form call must land HERE.
      CAMOUFOX_FORMS_URL: 'http://camoufox-forms.test:8000',
      SEARXNG_URL: 'http://searxng.test:8080',
    },
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      reportsDirectory: 'coverage',
      include: ['src/**/*.ts'],
      // Types-only and ambient declarations carry no runtime code.
      exclude: ['src/**/*.d.ts', 'src/types.ts'],
      // `pnpm test:coverage` (and CI) fails below these.
      thresholds: { lines: 80, branches: 70 },
    },
  },
});

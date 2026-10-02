import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { forcedWaitMs, isItalianSource, pickBackend } from '../src/routing.js';
import { MAX_FETCH_TIMEOUT_MS } from '../src/schemas.js';

describe('pickBackend', () => {
  it('sends Italian sources to camoufox', () => {
    expect(pickBackend('https://www.consob.it/web/area-pubblica')).toBe('camoufox');
    expect(pickBackend('https://giustiziatributaria.gov.it/')).toBe('camoufox');
    expect(pickBackend('https://www.altalex.com/documents')).toBe('camoufox');
    expect(isItalianSource('https://altalex.com/')).toBe(true);
  });

  it('sends challenge hosts scrapling cannot clear to camoufox', () => {
    expect(pickBackend('https://www.trustpilot.com/review/gorgias.com')).toBe('camoufox');
    expect(pickBackend('https://trustpilot.com/')).toBe('camoufox');
  });

  it('sends everything else to scrapling', () => {
    expect(pickBackend('https://example.com/')).toBe('scrapling');
    expect(pickBackend('https://www.linkedin.com/in/x')).toBe('scrapling');
    // label boundaries: these are not altalex.com / trustpilot.com / .it
    expect(pickBackend('https://notaltalex.com/')).toBe('scrapling');
    expect(pickBackend('https://faketrustpilot.com/')).toBe('scrapling');
    expect(pickBackend('https://example.italia.com/')).toBe('scrapling');
    expect(pickBackend('not a url')).toBe('scrapling');
  });
});

describe('host rules', () => {
  it('matches hosts case-insensitively and on any subdomain', () => {
    expect(pickBackend('https://WWW.CONSOB.IT/')).toBe('camoufox');
    expect(pickBackend('https://a.b.altalex.com/x')).toBe('camoufox');
    expect(pickBackend('https://uk.TrustPilot.com/review/x')).toBe('camoufox');
    expect(forcedWaitMs('https://uk.trustpilot.com/review/x')).toBe(20_000);
  });

  it('.it is a suffix on the hostname only, not the path, port or a longer TLD', () => {
    expect(pickBackend('https://example.com/page.it')).toBe('scrapling');
    expect(pickBackend('https://example.com:8443/?q=.it')).toBe('scrapling');
    expect(pickBackend('https://example.it.com/')).toBe('scrapling');
    expect(pickBackend('http://localhost.it:8080/')).toBe('camoufox');
    expect(isItalianSource('https://example.com/')).toBe(false);
  });

  it('a country-specific trustpilot under .it is Italian and still forced to wait', () => {
    expect(pickBackend('https://it.trustpilot.com/')).toBe('camoufox');
    expect(pickBackend('https://trustpilot.it/')).toBe('camoufox');
    expect(forcedWaitMs('https://trustpilot.it/')).toBeUndefined();
  });

  it('an unparseable URL is never Italian and never forced', () => {
    expect(isItalianSource('consob.it')).toBe(false);
    expect(forcedWaitMs('trustpilot.com')).toBeUndefined();
  });
});

describe('forcedWaitMs', () => {
  it('forces the measured 20s settle on trustpilot only', () => {
    expect(forcedWaitMs('https://www.trustpilot.com/review/x')).toBe(20_000);
    expect(forcedWaitMs('https://www.consob.it/')).toBeUndefined();
    expect(forcedWaitMs('https://example.com/')).toBeUndefined();
  });
});

describe('timeout cap', () => {
  it('matches the scrapling sidecar MAX_FETCH_MS', () => {
    const app = readFileSync(new URL('../../../services/scrapling/app.py', import.meta.url), 'utf8');
    const m = app.match(/^MAX_FETCH_MS = ([\d_]+)$/m);
    expect(m).not.toBeNull();
    expect(Number(m![1]!.replace(/_/g, ''))).toBe(MAX_FETCH_TIMEOUT_MS);
  });
});

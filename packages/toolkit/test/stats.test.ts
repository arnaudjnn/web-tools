// Usage counters: every tool counted, errors and blocks counted, proxy cost
// estimated only from the tools that can ride a metered exit.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { functionMap } from '../src/functions.js';
import { getStats, recordCall, resetStats } from '../src/stats.js';
import { TOOL_NAMES } from '../src/types.js';
import { fakeSidecars } from './sidecars.js';

beforeEach(() => resetStats());
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('stats counters', () => {
  it('has a zeroed counter for every tool', () => {
    const s = getStats();
    expect(Object.keys(s.by_tool).sort()).toEqual([...TOOL_NAMES].sort());
    expect(s).toMatchObject({ total_calls: 0, total_errors: 0, proxy_calls: 0, response_bytes: 0 });
    expect(Date.parse(s.started_at)).not.toBeNaN();
  });

  it('sums calls, bytes and errors; only proxy-backed tools count toward cost', () => {
    vi.stubEnv('PROXY_USD_PER_GB', '4');
    vi.stubEnv('PROXY_BYTES_MULTIPLIER', '2');
    recordCall('web_fetch', 1024 ** 3 / 2);
    recordCall('web_fetch', 0, true);
    recordCall('web_search', 999); // SearXNG: not proxy-backed
    recordCall('web_archive', 1024 ** 3 / 2); // residential /raw: proxy-backed
    const s = getStats();
    expect(s.by_tool.web_fetch).toEqual({ calls: 2, bytes: 1024 ** 3 / 2, errors: 1 });
    expect(s.total_calls).toBe(4);
    expect(s.total_errors).toBe(1);
    expect(s.proxy_calls).toBe(3);
    expect(s.response_bytes).toBe(1024 ** 3);
    expect(s.proxy_bytes).toBe(2 * 1024 ** 3);
    expect(s.proxy_gb).toBe(2);
    expect(s.estimated_usd).toBe(8);
    expect(s).toMatchObject({ rate_per_gb_usd: 4, bytes_multiplier: 2 });
  });

  it('defaults to $10/GB and an 8x upstream multiplier', () => {
    const s = getStats();
    expect(s).toMatchObject({ rate_per_gb_usd: 10, bytes_multiplier: 8 });
  });

  it('a returned copy cannot mutate the counters', () => {
    recordCall('web_html', 10);
    getStats().by_tool.web_html.calls = 100;
    expect(getStats().by_tool.web_html.calls).toBe(1);
  });

  it('resetStats zeroes everything', () => {
    recordCall('web_html', 10, true);
    resetStats();
    expect(getStats().by_tool.web_html).toEqual({ calls: 0, bytes: 0, errors: 0 });
  });
});

describe('instrument() counts through the function map', () => {
  it('counts the payload bytes of text, image and resource content', async () => {
    fakeSidecars({ scrapling: (p) => ({ json: { status: 200, url: 'https://e.com/', mode: 'fast', b64: p === '/pdf' ? 'PDF64' : 'PNG' } }) });
    await functionMap.web_screenshot({ url: 'https://e.com/' });
    await functionMap.web_pdf({ url: 'https://e.com/' });
    const s = getStats().by_tool;
    expect(s.web_screenshot).toEqual({ calls: 1, bytes: 3, errors: 0 });
    expect(s.web_pdf).toEqual({ calls: 1, bytes: 5, errors: 0 });
  });

  it('counts a challenge page handed back as content as an error', async () => {
    const html = '<html><head><title>Just a moment...</title></head><body>cf</body></html>';
    fakeSidecars({ scrapling: (p, b) => ({ json: { status: 200, url: b.url, html, size: html.length, mode: 'fast', escalated: false } }) });
    const r = await functionMap.web_html({ url: 'https://e.com/' });
    expect(r.isError).toBe(false); // web_html reports status as data …
    expect(getStats().by_tool.web_html).toMatchObject({ calls: 1, errors: 1 }); // … but the block is counted
  });

  it('a throw becomes an isError result and a counted error with zero bytes', async () => {
    fakeSidecars({});
    const r = await functionMap.web_bytes({ url: 'https://e.com/a.pdf' });
    expect(r.isError).toBe(true);
    expect(r.content[0]).toMatchObject({ type: 'text', text: expect.stringMatching(/^web_bytes error: camoufox \/bytes unreachable/) });
    expect(getStats().by_tool.web_bytes).toEqual({ calls: 1, bytes: 0, errors: 1 });
  });
});

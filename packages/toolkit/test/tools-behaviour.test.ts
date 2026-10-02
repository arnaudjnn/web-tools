// Tool behaviour beyond the fallback: web_crawl limits and deadline, how each
// tool's options reach the sidecar, and the edge results.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { camoufoxScreenshot, camoufoxSpaFetch } from '../src/camoufox.js';
import { functionMap } from '../src/functions.js';
import { CRAWL_DEADLINE_MS } from '../src/schemas.js';
import { scraplingFetch, scraplingRaw } from '../src/scrapling.js';
import { fakeSidecars } from './sidecars.js';

const HTML = '<html><body><h1>Hi</h1><p class="keep">kept</p><p>dropped</p></body></html>';
const page = (url: string, status = 200, html = HTML) => ({
  json: { status, url, html, size: html.length, mode: 'fast', escalated: false },
});
const bodyOf = (r: { content: Array<{ type: string; text?: string }> }) => JSON.parse(r.content[0]!.text!);
const textOf = (r: { content: Array<{ type: string; text?: string }> }) => r.content[0]!.text!;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('web_crawl', () => {
  it('is sequential, in order, and renders through the sidecar when it is up', async () => {
    const calls = fakeSidecars({
      scrapling: (p, b) => (p === '/fetch' ? page(b.url) : { json: { markdown: `md of ${b.url}` } }),
    });
    const r = await functionMap.web_crawl({ urls: ['https://a.com/', 'https://b.com/'], css_selector: '.keep' });
    expect(r.isError).toBe(false);
    expect(bodyOf(r).results).toEqual([
      { url: 'https://a.com/', status_code: 200, success: true, mode: 'fast', renderer: 'scrapling', markdown: 'md of https://a.com/' },
      { url: 'https://b.com/', status_code: 200, success: true, mode: 'fast', renderer: 'scrapling', markdown: 'md of https://b.com/' },
    ]);
    expect(calls.map((c) => `${c.path} ${c.body.url}`)).toEqual([
      '/fetch https://a.com/', '/markdown https://a.com/', '/fetch https://b.com/', '/markdown https://b.com/',
    ]);
    expect(calls[1]!.body).toMatchObject({ filter: 'fit', css_selector: '.keep' });
  });

  it('a blocked or empty page is success:false; all failing makes the call isError', async () => {
    fakeSidecars({
      scrapling: (p, b) => (p === '/fetch' ? (b.url.includes('empty') ? page(b.url, 200, '') : page(b.url, 403)) : { json: { markdown: 'wall' } }),
    });
    const r = await functionMap.web_crawl({ urls: ['https://blocked.com/', 'https://empty.com/'] });
    expect(r.isError).toBe(true);
    const [blocked, empty] = bodyOf(r).results;
    expect(blocked).toMatchObject({ status_code: 403, success: false, markdown: 'wall' });
    expect(empty).toEqual({ url: 'https://empty.com/', status_code: 200, success: false, mode: 'fast', markdown: '' });
  });

  it('shrinks each fetch to fit the 300 s deadline, then reports unreached URLs in their slots', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-01T00:00:00Z'));
    const calls = fakeSidecars({
      scrapling: (p, b) => {
        if (p === '/fetch') vi.setSystemTime(Date.now() + 120_000); // each fetch takes 2 minutes
        return p === '/fetch' ? page(b.url) : { json: { markdown: 'md' } };
      },
    });
    const urls = ['https://1.com/', 'https://2.com/', 'https://3.com/'];
    const r = await functionMap.web_crawl({ urls, timeout_ms: 90_000 });
    const fetches = calls.filter((c) => c.path === '/fetch');
    // 300 s budget − 30 s client overhead: 90 s, then min(90, 300−120−30=150) = 90 s,
    // then 300−240−30 = 30 s left.
    expect(fetches.map((c) => c.body.timeout_ms)).toEqual([90_000, 90_000, 30_000]);
    expect(bodyOf(r).results.map((x: { success: boolean }) => x.success)).toEqual([true, true, true]);

    vi.setSystemTime(new Date('2026-10-01T00:00:00Z'));
    calls.length = 0;
    const late = await functionMap.web_crawl({ urls: [...urls, 'https://4.com/'], timeout_ms: 90_000 });
    const results = bodyOf(late).results;
    expect(results[3]).toEqual({
      url: 'https://4.com/', status_code: 0, success: false,
      error: 'scrapling: deadline exceeded before the fetch could start; camoufox: deadline exceeded before the fetch could start',
    });
    expect(calls.filter((c) => c.path === '/fetch')).toHaveLength(3);
    expect(CRAWL_DEADLINE_MS).toBe(300_000);
  });
});

describe('web_fetch', () => {
  it('passes an explicit delay as wait_ms and the filter to /markdown; no hidden delay', async () => {
    const calls = fakeSidecars({ scrapling: (p, b) => (p === '/fetch' ? page(b.url) : { json: { markdown: 'ok' } }) });
    await functionMap.web_fetch({ url: 'https://e.com/', delay: 1.5, f: 'raw' });
    expect(calls[0]!.body).toMatchObject({ wait_ms: 1500, timeout_ms: 60_000, network_idle: false });
    expect(calls[1]!.body).toMatchObject({ filter: 'raw' });
    calls.length = 0;
    await functionMap.web_fetch({ url: 'https://e.com/' });
    expect(calls[0]!.body.wait_ms).toBe(0);
  });

  it('an empty page is an error that says so', async () => {
    fakeSidecars({ scrapling: (p, b) => (p === '/fetch' ? page(b.url, 200, '') : { json: { markdown: '' } }) });
    const r = await functionMap.web_fetch({ url: 'https://e.com/' });
    expect(r.isError).toBe(true);
    expect(textOf(r)).toBe('web_fetch: upstream returned HTTP 200 with no extractable content (0 bytes, mode=fast).');
  });

  it('notes an escalated fetch in the provenance', async () => {
    fakeSidecars({
      scrapling: (p, b) => (p === '/fetch' ? { json: { ...page(b.url, 403).json, mode: 'solve', escalated: true } } : { json: { markdown: 'wall' } }),
    });
    expect(textOf(await functionMap.web_fetch({ url: 'https://e.com/' }))).toMatch(/\(mode=solve, escalated\)/);
  });
});

describe('web_html', () => {
  it('maps network_idle for scrapling and returns status as data', async () => {
    const calls = fakeSidecars({ scrapling: (p, b) => page(b.url, 404) });
    const r = await functionMap.web_html({ url: 'https://e.com/', network_idle: true, wait_ms: 50, timeout_ms: 5000 });
    expect(r.isError).toBe(false);
    expect(bodyOf(r)).toEqual({ status: 404, url: 'https://e.com/', mode: 'fast', escalated: false, size: HTML.length, html: HTML });
    expect(calls[0]!.body).toEqual({ url: 'https://e.com/', network_idle: true, timeout_ms: 5000, wait_ms: 50 });
  });

  it('passes the page options to camoufox for an Italian host', async () => {
    const calls = fakeSidecars({ camoufox: (p, b) => ({ json: { status: 200, url: b.url, html: HTML } }) });
    await functionMap.web_html({
      url: 'https://www.consob.it/', wait_until: 'domcontentloaded', wait_ms: 10, click_all: ['.acc'], settle_ms: 20, fresh_ip: true,
    });
    expect(calls[0]!.body).toEqual({
      url: 'https://www.consob.it/', wait_until: 'domcontentloaded', wait_ms: 10, timeout_ms: 60_000,
      click_all: ['.acc'], settle_ms: 20, fresh_ip: true,
    });
  });
});

describe('captures and camoufox-only tools', () => {
  it('web_screenshot: wait seconds → ms (default 2 s); an upstream 4xx is isError', async () => {
    const calls = fakeSidecars({ scrapling: (p, b) => ({ json: { status: 403, url: b.url, mode: 'fast', b64: 'x' } }) });
    const r = await functionMap.web_screenshot({ url: 'https://e.com/', screenshot_wait_for: 0.5 });
    expect(r.isError).toBe(true);
    expect(calls[0]!.body.wait_ms).toBe(500);
    await functionMap.web_screenshot({ url: 'https://e.com/' });
    expect(calls[1]!.body.wait_ms).toBe(2000);
  });

  it('web_pdf on an ordinary host prints live through scrapling', async () => {
    const calls = fakeSidecars({ scrapling: (p, b) => ({ json: { status: 500, url: b.url, mode: 'fast', b64: 'JV' } }) });
    const r = await functionMap.web_pdf({ url: 'https://e.com/' });
    expect(r.isError).toBe(true);
    expect(calls).toEqual([{ host: 'scrapling', path: '/pdf', body: { url: 'https://e.com/', wait_ms: 0, timeout_ms: 60_000 } }]);
  });

  it('web_execute_js wraps a single camoufox result in an array', async () => {
    fakeSidecars({ camoufox: () => ({ json: { status: 200, url: 'https://www.ivass.it/', result: 'only' } }) });
    expect(bodyOf(await functionMap.web_execute_js({ url: 'https://www.ivass.it/', scripts: ['1'] })).results).toEqual(['only']);
  });

  it('web_execute_js through scrapling returns its results as they are', async () => {
    const calls = fakeSidecars({ scrapling: () => ({ json: { status: 200, url: 'https://e.com/', mode: 'fast', results: [1, 2] } }) });
    expect(bodyOf(await functionMap.web_execute_js({ url: 'https://e.com/', scripts: ['1', '2'] }))).toEqual({
      status: 200, url: 'https://e.com/', mode: 'fast', results: [1, 2],
    });
    expect(calls[0]!.body).toEqual({ url: 'https://e.com/', scripts: ['1', '2'], wait_ms: 0, timeout_ms: 60_000 });
  });

  it('web_bytes reports the status as data and the base64 size', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: { status: 404, b64: 'QUJD' } }) });
    const r = await functionMap.web_bytes({ url: 'https://e.com/a.pdf', timeout_ms: 5000 });
    expect(r.isError).toBe(false);
    expect(bodyOf(r)).toEqual({ status: 404, url: 'https://e.com/a.pdf', size_b64: 4, b64: 'QUJD' });
    expect(calls[0]!.body).toEqual({ url: 'https://e.com/a.pdf', timeout_ms: 5000 });
  });

  it('web_eval forwards its options', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: { status: 200, url: 'https://e.com/', result: 42 } }) });
    const r = await functionMap.web_eval({ url: 'https://e.com/', js: '6*7', wait_until: 'load', wait_ms: 1, timeout_ms: 2000, fresh_ip: true });
    expect(bodyOf(r)).toEqual({ status: 200, url: 'https://e.com/', result: 42 });
    expect(calls[0]!.body).toEqual({ url: 'https://e.com/', js: '6*7', wait_until: 'load', wait_ms: 1, timeout_ms: 2000, fresh_ip: true });
  });

  it('web_spa_fetch forwards its options and returns the upstream status as data', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: { status: 403, text: 'sensor' } }) });
    const r = await functionMap.web_spa_fetch({
      base_url: 'https://spa.example.it', path: '/api', warm_path: '/', method: 'POST', body: { q: 1 }, accept: 'application/json',
      sensor_wait_ms: 100, mature_probe: { path: '/p' }, mature_max_tries: 2, timeout_ms: 10_000,
    });
    expect(bodyOf(r)).toEqual({ status: 403, text: 'sensor' });
    expect(calls[0]!.body).toEqual({
      base_url: 'https://spa.example.it', warm_path: '/', method: 'POST', path: '/api', body: { q: 1 }, accept: 'application/json',
      sensor_wait_ms: 100, mature_probe: { path: '/p' }, mature_max_tries: 2,
    });
  });

  it('web_recycle posts /recycle', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: { ok: true } }) });
    expect(bodyOf(await functionMap.web_recycle({}))).toEqual({ ok: true });
    expect(calls).toEqual([{ host: 'camoufox', path: '/recycle', body: {} }]);
  });
});

describe('client wire format', () => {
  it('camoufoxScreenshot sends only the options given', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: {} }) });
    await camoufoxScreenshot({ url: 'https://e.com/' });
    await camoufoxScreenshot({
      url: 'https://e.com/', waitUntil: 'load', waitMs: 1, timeoutMs: 2, fullPage: false, width: 800, height: 600,
      clickAll: ['.a'], settleMs: 3, freshIp: true,
    });
    expect(calls.map((c) => c.body)).toEqual([
      { url: 'https://e.com/', timeout_ms: 90_000 },
      { url: 'https://e.com/', wait_until: 'load', wait_ms: 1, timeout_ms: 2, full_page: false, width: 800, height: 600, click_all: ['.a'], settle_ms: 3, fresh_ip: true },
    ]);
  });

  it('camoufoxSpaFetch defaults: a 180 s client budget and only base_url + path', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: {} }) });
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    await camoufoxSpaFetch({ baseUrl: 'https://s.it', path: '/x' });
    expect(calls[0]!.body).toEqual({ base_url: 'https://s.it', path: '/x' });
    expect(timeout).toHaveBeenCalledWith(180_000);
    timeout.mockRestore();
  });

  it('scrapling: an explicit mode is forwarded; defaults otherwise', async () => {
    const calls = fakeSidecars({ scrapling: () => ({ json: {} }) });
    await scraplingFetch({ url: 'https://e.com/', mode: 'stealth' });
    await scraplingRaw({ url: 'https://e.com/', mode: 'fast' });
    await scraplingRaw({ url: 'https://e.com/' });
    expect(calls.map((c) => c.body)).toEqual([
      { url: 'https://e.com/', mode: 'stealth', network_idle: false, timeout_ms: 60_000, wait_ms: 0 },
      { url: 'https://e.com/', mode: 'fast', timeout_ms: 60_000 },
      { url: 'https://e.com/', timeout_ms: 60_000 },
    ]);
  });
});

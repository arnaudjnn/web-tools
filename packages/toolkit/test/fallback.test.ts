import { afterEach, describe, expect, it, vi } from 'vitest';
import { functionMap } from '../src/functions.js';
import { getStats } from '../src/stats.js';
import { fakeSidecars } from './sidecars.js';

const HTML = '<html><body><h1>Hello</h1><a href="/next">next</a></body></html>';

const scraplingPage = (url: string, mode = 'fast', status = 200) => ({
  json: { status, url, html: HTML, size: HTML.length, mode, escalated: false },
});
const camoufoxPage = (url: string, status = 200) => ({ json: { status, url, html: HTML } });

const bodyOf = (r: { content: Array<{ type: string; text?: string }> }) => JSON.parse(r.content[0]!.text!);

afterEach(() => vi.unstubAllGlobals());

describe('fetchPage symmetric fallback', () => {
  it('serves through scrapling for an ordinary host', async () => {
    const calls = fakeSidecars({ scrapling: (p, b) => scraplingPage(b.url) });
    const r = await functionMap.web_html({ url: 'https://example.com/' });
    expect(r.isError).toBe(false);
    expect(bodyOf(r)).toMatchObject({ status: 200, mode: 'fast' });
    expect(calls.map((c) => c.host)).toEqual(['scrapling']);
  });

  it('falls back to camoufox when scrapling is unreachable, then skips the dead sidecar', async () => {
    const calls = fakeSidecars({ camoufox: (p, b) => camoufoxPage(b.url) });
    const r = await functionMap.web_html({ url: 'https://example.com/' });
    expect(bodyOf(r)).toMatchObject({ status: 200, mode: 'camoufox' });
    expect(calls.map((c) => `${c.host}${c.path}`)).toEqual(['scrapling/fetch', 'camoufox/render']);

    // Breaker open: the second call goes straight to camoufox.
    await functionMap.web_html({ url: 'https://example.com/again' });
    expect(calls.slice(2).map((c) => c.host)).toEqual(['camoufox']);
  });

  it('falls back to scrapling when camoufox fails on an Italian host', async () => {
    const calls = fakeSidecars({
      camoufox: () => ({ status: 502, text: 'NS_ERROR_CONNECTION_REFUSED' }),
      scrapling: (p, b) => scraplingPage(b.url),
    });
    const r = await functionMap.web_html({ url: 'https://www.consob.it/' });
    expect(bodyOf(r)).toMatchObject({ mode: 'fast' });
    expect(calls.map((c) => c.host)).toEqual(['camoufox', 'scrapling']);
  });

  it('forces the measured wait on trustpilot via camoufox', async () => {
    const calls = fakeSidecars({ camoufox: (p, b) => camoufoxPage(b.url) });
    await functionMap.web_html({ url: 'https://www.trustpilot.com/review/x', wait_ms: 1000 });
    expect(calls[0]).toMatchObject({ host: 'camoufox', path: '/render', body: { wait_ms: 20_000 } });
  });

  it('names both causes when both sidecars fail', async () => {
    fakeSidecars({ camoufox: () => ({ status: 502, text: 'render died' }) });
    const r = await functionMap.web_html({ url: 'https://example.com/' });
    expect(r.isError).toBe(true);
    const msg = r.content[0]!.type === 'text' ? r.content[0]!.text : '';
    expect(msg).toMatch(/^web_html error: scrapling: .*ECONNREFUSED.*; camoufox: .*HTTP 502: render died/);
    expect(getStats().by_tool.web_html.errors).toBe(1);
  });

  it('does not replay a request the sidecar refused as invalid (4xx)', async () => {
    const calls = fakeSidecars({ scrapling: () => ({ status: 400, text: 'capture failed: ReferenceError' }) });
    const r = await functionMap.web_execute_js({ url: 'https://example.com/', scripts: ['nope()'] });
    expect(r.isError).toBe(true);
    expect(calls.map((c) => c.host)).toEqual(['scrapling']);
  });
});

describe('web_fetch survives a scrapling outage', () => {
  it('fetches via camoufox and renders markdown locally', async () => {
    const calls = fakeSidecars({ camoufox: () => camoufoxPage('https://example.com/a/') });
    const r = await functionMap.web_fetch({ url: 'https://example.com/a' });
    expect(r.isError).toBe(false);
    expect(r.content[0]).toEqual({ type: 'text', text: '# Hello\n\n[next](https://example.com/next)' });
    // /markdown is skipped by the open breaker; no third hop.
    expect(calls.map((c) => `${c.host}${c.path}`)).toEqual(['scrapling/fetch', 'camoufox/render']);
  });

  it('keeps the body but flags a blocked page', async () => {
    fakeSidecars({
      scrapling: (p, b) => (p === '/fetch' ? scraplingPage(b.url, 'fast', 403) : { json: { markdown: 'wall' } }),
    });
    const r = await functionMap.web_fetch({ url: 'https://example.com/' });
    expect(r.isError).toBe(true);
    expect(r.content[0]).toMatchObject({ text: expect.stringMatching(/HTTP 403 \(mode=fast\).*\n\nwall$/s) });
  });
});

describe('web_crawl', () => {
  it('reports the renderer and keeps failed URLs in their own slot', async () => {
    fakeSidecars({
      camoufox: (p, b) => (b.url.includes('bad') ? { status: 502, text: 'boom' } : camoufoxPage(b.url)),
    });
    const r = await functionMap.web_crawl({ urls: ['https://example.com/', 'https://bad.example.com/'] });
    const { results } = bodyOf(r);
    expect(r.isError).toBe(false);
    expect(results[0]).toMatchObject({ url: 'https://example.com/', success: true, mode: 'camoufox', renderer: 'local' });
    expect(results[1]).toMatchObject({ url: 'https://bad.example.com/', status_code: 0, success: false });
    expect(results[1].error).toMatch(/scrapling: .*; camoufox: .*502/);
  });
});

describe('captures', () => {
  it('web_screenshot returns MCP image content, and keeps both errors', async () => {
    fakeSidecars({ scrapling: () => ({ json: { status: 200, url: 'https://example.com/', mode: 'fast', b64: 'iVBOR' } }) });
    const ok = await functionMap.web_screenshot({ url: 'https://example.com/' });
    expect(ok).toEqual({ content: [{ type: 'image', data: 'iVBOR', mimeType: 'image/png' }], isError: false });

    fakeSidecars({ camoufox: () => ({ status: 502, text: 'shot failed' }) });
    const bad = await functionMap.web_screenshot({ url: 'https://example.com/' });
    expect(bad.isError).toBe(true);
    expect(bad.content[0]).toMatchObject({ text: expect.stringMatching(/scrapling: .*ECONNREFUSED.*; camoufox: .*shot failed/) });
  });

  it('web_pdf on an Italian host: camoufox renders, scrapling prints that DOM', async () => {
    const calls = fakeSidecars({
      camoufox: () => camoufoxPage('https://www.consob.it/final', 200),
      scrapling: () => ({ json: { status: 200, url: 'https://www.consob.it/final', mode: 'fast', b64: 'JVBER' } }),
    });
    const r = await functionMap.web_pdf({ url: 'https://www.consob.it/' });
    expect(r.content[0]).toEqual({
      type: 'resource',
      resource: { uri: 'https://www.consob.it/', mimeType: 'application/pdf', blob: 'JVBER' },
    });
    expect(calls[1]).toMatchObject({ host: 'scrapling', path: '/pdf', body: { url: 'https://www.consob.it/final', html: HTML } });
  });

  it('web_execute_js on an Italian host sequences the scripts in one camoufox eval', async () => {
    const calls = fakeSidecars({ camoufox: () => ({ json: { status: 200, url: 'https://www.ivass.it/', result: [1, 'two'] } }) });
    const r = await functionMap.web_execute_js({ url: 'https://www.ivass.it/', scripts: ['1', '() => "two"'] });
    expect(bodyOf(r)).toEqual({ status: 200, url: 'https://www.ivass.it/', mode: 'camoufox', results: [1, 'two'] });
    expect(calls[0]!.path).toBe('/eval');
    // The sequencer itself, evaluated here as the page would.
    expect(await (0, eval)(calls[0]!.body.js)).toEqual([1, 'two']);
  });
});

describe('errors are counted for every tool', () => {
  it('web_search: a SearXNG failure throws (no silent []) and is counted', async () => {
    fakeSidecars({ searxng: () => ({ status: 502, text: 'bad gateway' }) });
    const r = await functionMap.web_search({ query: 'x' });
    expect(r.isError).toBe(true);
    expect(r.data).toBeUndefined();
    expect(getStats().by_tool.web_search).toMatchObject({ calls: 1, errors: 1 });
  });

  it('web_search: zero results with engines down is an error, with healthy engines it is []', async () => {
    fakeSidecars({ searxng: () => ({ json: { results: [], unresponsive_engines: [['google', 'CAPTCHA']] } }) });
    const down = await functionMap.web_search({ query: 'x' });
    expect(down.content[0]).toMatchObject({ text: expect.stringContaining('google (CAPTCHA)') });

    fakeSidecars({ searxng: () => ({ json: { results: [] } }) });
    expect((await functionMap.web_search({ query: 'x' })).data).toEqual([]);
  });

  it('web_search returns deduplicated data', async () => {
    const results = [
      { url: 'https://a/', title: 'A', content: 'a' },
      { url: 'https://a/', title: 'A again', content: 'a' },
      { url: 'https://b/', title: 'B', content: '' },
    ];
    fakeSidecars({ searxng: () => ({ json: { results } }) });
    const r = await functionMap.web_search({ query: 'x', limit: 5 });
    expect(r.data).toEqual([
      { url: 'https://a/', title: 'A', description: 'a' },
      { url: 'https://b/', title: 'B', description: '' },
    ]);
  });

  it('web_snapshots / web_archive failures are counted', async () => {
    fakeSidecars({});
    expect((await functionMap.web_snapshots({ url: 'example.com' })).isError).toBe(true);
    expect((await functionMap.web_archive({ url: 'example.com', timestamp: '20200101000000' })).isError).toBe(true);
    const s = getStats().by_tool;
    expect(s.web_snapshots.errors).toBe(1);
    expect(s.web_archive.errors).toBe(1);
  });

  it('web_archive falls back to camoufox egress and local markdown', async () => {
    const html = '<html><body><p>archived <a href="/web/2020/x">x</a></p></body></html>';
    fakeSidecars({ camoufox: () => ({ json: { status: 200, b64: Buffer.from(html).toString('base64') } }) });
    const r = await functionMap.web_archive({ url: 'example.com', timestamp: '20200101000000' });
    expect(r.data).toMatchObject({ content: 'archived [x](https://web.archive.org/web/2020/x)' });
  });

  it('web_recycle and web_usage_stats are tracked tools', async () => {
    fakeSidecars({ camoufox: () => ({ json: { ok: true } }) });
    await functionMap.web_recycle({});
    await functionMap.web_usage_stats({});
    const s = getStats().by_tool;
    expect(s.web_recycle.calls).toBe(1);
    expect(s.web_usage_stats.calls).toBe(1);
  });
});

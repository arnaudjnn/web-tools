// SearXNG: a failure is an error (never a silent []), zero results with
// healthy engines is a real empty answer.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { searchSearXNG } from '../src/searxng.js';

type Reply = { status?: number; body: unknown } | Error;

function stub(reply: Reply) {
  const f = vi.fn(async (_input: URL | string, _init?: RequestInit) => {
    if (reply instanceof Error) throw reply;
    const body = typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body);
    return new Response(body, { status: reply.status ?? 200 });
  });
  vi.stubGlobal('fetch', f);
  return f;
}

afterEach(() => vi.unstubAllGlobals());

describe('searchSearXNG', () => {
  it('queries /search as JSON with the caller engines, Accept json and a 25 s deadline', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    const f = stub({ body: { results: [] } });
    await searchSearXNG('hello world', { engines: 'google,brave' });
    const url = new URL(String(f.mock.calls[0]![0]));
    expect(url.origin + url.pathname).toBe('http://searxng.test:8080/search');
    expect(url.searchParams.get('q')).toBe('hello world');
    expect(url.searchParams.get('format')).toBe('json');
    expect(url.searchParams.get('engines')).toBe('google,brave');
    expect(f.mock.calls[0]![1]).toMatchObject({ headers: { Accept: 'application/json' } });
    // Must stay above SearXNG's own 15 s request timeout.
    expect(timeout).toHaveBeenCalledWith(25_000);
    timeout.mockRestore();
  });

  it('omits engines when none is chosen (SearXNG defaults apply)', async () => {
    const f = stub({ body: { results: [] } });
    await searchSearXNG('x');
    expect(new URL(String(f.mock.calls[0]![0])).searchParams.has('engines')).toBe(false);
  });

  it('unreachable is an error', async () => {
    stub(Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } }));
    await expect(searchSearXNG('x')).rejects.toThrow('searxng unreachable: fetch failed');
  });

  it('an HTTP error is an error naming the status', async () => {
    stub({ status: 429, body: 'Too Many Requests' });
    await expect(searchSearXNG('x')).rejects.toThrow('searxng HTTP 429: Too Many Requests');
  });

  it('zero results with unresponsive engines is an error naming each engine and reason', async () => {
    stub({ body: { results: [], unresponsive_engines: [['google', 'CAPTCHA'], ['brave', 'timeout']] } });
    await expect(searchSearXNG('x')).rejects.toThrow(
      'searxng: no results, engines unresponsive: google (CAPTCHA), brave (timeout)',
    );
  });

  it('results with some engines down are still results', async () => {
    stub({ body: { results: [{ url: 'https://a/', title: 'A', content: 'a' }], unresponsive_engines: [['google', 'CAPTCHA']] } });
    expect(await searchSearXNG('x')).toEqual([{ url: 'https://a/', title: 'A', description: 'a' }]);
  });

  it('zero results with healthy engines, or no results key, is []', async () => {
    stub({ body: { results: [] } });
    expect(await searchSearXNG('x')).toEqual([]);
    stub({ body: {} });
    expect(await searchSearXNG('x')).toEqual([]);
  });

  it('drops results without a title or URL, dedupes by URL, and honours the limit (default 10)', async () => {
    const results = [
      { url: 'https://a/', title: '', content: 'untitled' },
      { url: '', title: 'no url', content: '' },
      ...Array.from({ length: 15 }, (_, i) => ({ url: `https://r${i % 12}/`, title: `R${i}`, content: undefined })),
    ];
    stub({ body: { results } });
    const ten = await searchSearXNG('x');
    expect(ten).toHaveLength(10);
    expect(ten[0]).toEqual({ url: 'https://r0/', title: 'R0', description: '' });
    stub({ body: { results } });
    expect(await searchSearXNG('x', { limit: 20 })).toHaveLength(12);
  });
});

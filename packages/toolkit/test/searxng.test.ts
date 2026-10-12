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
    await searchSearXNG('hello world', { engines: 'google' });
    const url = new URL(String(f.mock.calls[0]![0]));
    expect(url.origin + url.pathname).toBe('http://searxng.test:8080/search');
    expect(url.searchParams.get('q')).toBe('hello world');
    expect(url.searchParams.get('format')).toBe('json');
    expect(url.searchParams.get('engines')).toBe('google');
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

  describe('several engines', () => {
    // One request per engine, so a blocked engine cannot hide another's rows.
    function perEngine(replies: Record<string, Reply>) {
      const f = vi.fn(async (input: URL | string) => {
        const reply = replies[new URL(String(input)).searchParams.get('engines')!]!;
        if (reply instanceof Error) throw reply;
        return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200 });
      });
      vi.stubGlobal('fetch', f);
      return f;
    }
    const row = (u: string) => ({ url: `https://${u}/`, title: u, content: '' });

    it('asks each engine separately and merges round-robin in the given order', async () => {
      const f = perEngine({
        google: { body: { results: [row('g1'), row('g2')] } },
        bing: { body: { results: [row('b1'), row('g1')] } },
      });
      const out = await searchSearXNG('x', { engines: 'google, bing' });
      expect(f.mock.calls.map((c) => new URL(String(c[0])).searchParams.get('engines'))).toEqual(['google', 'bing']);
      expect(out.map((r) => r.url)).toEqual(['https://g1/', 'https://b1/', 'https://g2/']);
    });

    it('keeps the healthy engines when one fails', async () => {
      perEngine({
        google: { body: { results: [row('g1')] } },
        brave: { body: { results: [], unresponsive_engines: [['brave', 'too many requests']] } },
      });
      expect((await searchSearXNG('x', { engines: 'google,brave' })).map((r) => r.url)).toEqual(['https://g1/']);
    });

    it('is an error only when every engine fails', async () => {
      perEngine({
        brave: { body: { results: [], unresponsive_engines: [['brave', 'too many requests']] } },
        duckduckgo: { status: 504, body: 'timeout' },
      });
      await expect(searchSearXNG('x', { engines: 'brave,duckduckgo' })).rejects.toThrow('searxng: every engine failed');
    });
  });

  describe('answers from engines that were not asked', () => {
    it('drops rows from other engines', async () => {
      stub({ body: { results: [
        { url: 'https://g/', title: 'G', content: '', engines: ['google'] },
        { url: 'https://b/', title: 'B', content: '', engines: ['bing'] },
      ] } });
      expect((await searchSearXNG('x', { engines: 'google' })).map((r) => r.url)).toEqual(['https://g/']);
    });

    it('is an error when only other engines answered (the asked one is not active)', async () => {
      stub({ body: { results: [{ url: 'https://b/', title: 'B', content: '', engines: ['bing'] }] } });
      await expect(searchSearXNG('x', { engines: 'google' })).rejects.toThrow(
        'searxng: google did not answer; results came from bing instead',
      );
    });

    it('keeps rows without engine metadata when no engine was asked', async () => {
      stub({ body: { results: [{ url: 'https://b/', title: 'B', content: '', engines: ['bing'] }] } });
      expect(await searchSearXNG('x')).toHaveLength(1);
    });
  });
});

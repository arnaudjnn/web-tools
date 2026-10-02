// Wayback: CDX parsing and archived pages, always through a sidecar's
// residential egress (never fetched from this process).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { functionMap } from '../src/functions.js';
import { getArchivedPage, getSnapshots } from '../src/wayback.js';
import { fakeSidecars } from './sidecars.js';

const raw = (body: string, status = 200, url = 'https://web.archive.org/x') => ({
  json: { status, url, body, size: body.length, mode: 'stealth' },
});

const CDX = [
  ['timestamp', 'original', 'mimetype', 'statuscode', 'digest', 'length'],
  ['20200102030405', 'https://example.com/', 'text/html', '200', 'ABC', '1234'],
  ['2021', 'https://example.com/a'],
];

afterEach(() => vi.unstubAllGlobals());

describe('getSnapshots (CDX)', () => {
  it('parses rows after the header, with archive URL and formatted date', async () => {
    fakeSidecars({ scrapling: () => raw(JSON.stringify(CDX)) });
    expect(await getSnapshots({ url: 'example.com' })).toEqual([
      {
        timestamp: '20200102030405',
        original: 'https://example.com/',
        mimetype: 'text/html',
        statusCode: '200',
        digest: 'ABC',
        length: '1234',
        archiveUrl: 'https://web.archive.org/web/20200102030405/https://example.com/',
        formattedDate: '2020-01-02 03:04:05',
      },
      // Short rows are tolerated; a non-14-digit timestamp is passed through.
      {
        timestamp: '2021',
        original: 'https://example.com/a',
        mimetype: '',
        statusCode: '',
        digest: '',
        length: '',
        archiveUrl: 'https://web.archive.org/web/2021/https://example.com/a',
        formattedDate: '2021',
      },
    ]);
  });

  it('header only or an empty array is no snapshots', async () => {
    fakeSidecars({ scrapling: () => raw(JSON.stringify([CDX[0]])) });
    expect(await getSnapshots({ url: 'example.com' })).toEqual([]);
    fakeSidecars({ scrapling: () => raw('[]') });
    expect(await getSnapshots({ url: 'example.com' })).toEqual([]);
  });

  it('builds the CDX query from the options, through scrapling /raw', async () => {
    const calls = fakeSidecars({ scrapling: () => raw('[]') });
    await getSnapshots({
      url: 'example.com',
      from: '2020',
      to: '2021',
      limit: 5,
      matchType: 'prefix',
      filter: ['statuscode:200', 'mimetype:text/html'],
    });
    expect(calls[0]).toMatchObject({ host: 'scrapling', path: '/raw', body: { timeout_ms: 60_000 } });
    const q = new URL(calls[0]!.body.url);
    expect(q.origin + q.pathname).toBe('https://web.archive.org/cdx/search/cdx');
    expect(q.searchParams.get('url')).toBe('example.com');
    expect(q.searchParams.get('output')).toBe('json');
    expect(q.searchParams.get('limit')).toBe('5');
    expect(q.searchParams.get('from')).toBe('2020');
    expect(q.searchParams.get('to')).toBe('2021');
    expect(q.searchParams.get('matchType')).toBe('prefix');
    expect(q.searchParams.getAll('filter')).toEqual(['statuscode:200', 'mimetype:text/html']);
  });

  it('exact match is the default and is not sent', async () => {
    const calls = fakeSidecars({ scrapling: () => raw('[]') });
    await getSnapshots({ url: 'example.com' });
    const q = new URL(calls[0]!.body.url).searchParams;
    expect(q.has('matchType')).toBe(false);
    expect(q.get('limit')).toBe('100');
  });

  it('a non-2xx upstream status is an error', async () => {
    fakeSidecars({ scrapling: () => raw('slow down', 429) });
    await expect(getSnapshots({ url: 'example.com' })).rejects.toThrow('Wayback CDX API error: 429');
  });

  it('an HTML error body with a 200 is an error, not an empty list', async () => {
    fakeSidecars({ scrapling: () => raw('<html><body>Wayback Machine is under maintenance</body></html>') });
    await expect(getSnapshots({ url: 'example.com' })).rejects.toThrow(SyntaxError);
    const r = await functionMap.web_snapshots({ url: 'example.com' });
    expect(r.isError).toBe(true);
    expect(r.data).toBeUndefined();
  });

  it('falls back to camoufox /bytes when scrapling cannot answer', async () => {
    const calls = fakeSidecars({
      camoufox: () => ({ json: { status: 200, b64: Buffer.from(JSON.stringify(CDX)).toString('base64') } }),
    });
    expect(await getSnapshots({ url: 'example.com' })).toHaveLength(2);
    expect(calls.map((c) => `${c.host}${c.path}`)).toEqual(['scrapling/raw', 'camoufox/bytes']);
  });

  it('names both causes when both egresses fail', async () => {
    fakeSidecars({ camoufox: () => ({ status: 502, text: 'exit down' }) });
    await expect(getSnapshots({ url: 'example.com' })).rejects.toThrow(
      /^scrapling: .*ECONNREFUSED.*; camoufox: .*HTTP 502: exit down/,
    );
  });
});

describe('getArchivedPage', () => {
  const page = '<html><body><p>old <a href="/web/2020/https://example.com/b">b</a></p></body></html>';

  it('fetches the wayback URL and resolves links against the final (redirected) URL', async () => {
    const calls = fakeSidecars({
      scrapling: (p) =>
        p === '/raw' ? raw(page, 200, 'https://web.archive.org/web/20200101000000/https://example.com/') : { status: 500 },
    });
    const r = await getArchivedPage({ url: 'https://example.com/', timestamp: '2020' });
    expect(r).toEqual({
      waybackUrl: 'https://web.archive.org/web/2020/https://example.com/',
      content: 'old [b](https://web.archive.org/web/2020/https://example.com/b)',
    });
    expect(calls[0]!.body).toMatchObject({ url: 'https://web.archive.org/web/2020/https://example.com/', timeout_ms: 90_000 });
  });

  it('original=true asks for the id_ (banner-free) capture', async () => {
    const calls = fakeSidecars({ scrapling: (p) => (p === '/raw' ? raw(page) : { json: { markdown: 'md' } }) });
    const r = await getArchivedPage({ url: 'example.com', timestamp: '2020', original: true });
    expect(r.waybackUrl).toBe('https://web.archive.org/web/id_2020/example.com');
    expect(calls[0]!.body.url).toBe(r.waybackUrl);
    expect(r.content).toBe('md');
    // The archive is rendered with filter raw (whole document).
    expect(calls[1]).toMatchObject({ path: '/markdown', body: { filter: 'raw' } });
  });

  it('a non-2xx capture is an error', async () => {
    fakeSidecars({ scrapling: () => raw('gone', 404) });
    await expect(getArchivedPage({ url: 'example.com', timestamp: '2020' })).rejects.toThrow('Wayback fetch error: 404');
  });

  it('web_archive truncates content at 50 000 characters', async () => {
    const long = 'x'.repeat(60_000);
    fakeSidecars({ scrapling: (p) => (p === '/raw' ? raw('<p>x</p>') : { json: { markdown: long } }) });
    const r = await functionMap.web_archive({ url: 'example.com', timestamp: '2020' });
    const d = r.data as { contentLength: number; content: string };
    expect(d.contentLength).toBe(60_000);
    expect(d.content).toBe('x'.repeat(50_000) + '\n\n[Content truncated]');
  });
});

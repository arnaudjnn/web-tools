import { afterEach, describe, expect, it, vi } from 'vitest';
import { printResult, runTool } from '../src/run.js';
import { capture, Exit, fakeBackend, run } from './helpers.js';

const PAGE = '<html><body><h1>T</h1></body></html>';
const fetched = (url: string) => ({ json: { status: 200, url, html: PAGE, size: PAGE.length, mode: 'fast', escalated: false } });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('runTool / printResult', () => {
  it('validates like REST: invalid params print the issues and exit 1 before any call', async () => {
    const { err } = capture();
    const calls = fakeBackend(() => ({ json: {} }));
    await expect(runTool('web_fetch', { url: 'nope' })).rejects.toEqual(new Exit(1));
    expect(err[0]).toMatch(/^invalid_params\n\[/);
    expect(err[0]).toContain('"url"');
    expect(calls).toHaveLength(0);
  });

  it('an unknown tool exits 1', async () => {
    const { err } = capture();
    await expect(runTool('web_nope', {})).rejects.toEqual(new Exit(1));
    expect(err).toEqual(['Unknown tool: web_nope']);
  });

  it('a tool error prints it to stderr and exits 1', async () => {
    const { err } = capture();
    fakeBackend(() => ({ status: 502, text: 'bad gateway' }));
    await expect(runTool('web_search', { query: 'x' })).rejects.toEqual(new Exit(1));
    expect(err[0]).toMatch(/^web_search error: searxng HTTP 502: bad gateway/);
  });

  it('printResult prints text, and base64 for image and resource blocks', () => {
    const { out } = capture();
    printResult({
      content: [
        { type: 'text', text: 't' },
        { type: 'image', data: 'PNG', mimeType: 'image/png' },
        { type: 'resource', resource: { uri: 'u', mimeType: 'application/pdf', blob: 'PDF' } },
      ],
    });
    expect(out).toEqual(['t', 'PNG', 'PDF']);
  });
});

describe('search', () => {
  const results = [
    { url: 'https://a/', title: 'A', content: 'about a' },
    { url: 'https://b/', title: 'B', content: '' },
  ];

  it('passes limit and engines, and pretty-prints', async () => {
    const { out } = capture();
    const calls = fakeBackend(() => ({ json: { results } }));
    await run('search', 'hello', '-l', '5', '-e', 'brave');
    expect(calls[0]!.url.searchParams.get('q')).toBe('hello');
    expect(calls[0]!.url.searchParams.get('engines')).toBe('brave');
    expect(out).toEqual(['1. A', '   https://a/', '   about a', '', '2. B', '   https://b/', '']);
  });

  it('--json prints the raw results; none says so', async () => {
    const { out } = capture();
    fakeBackend(() => ({ json: { results } }));
    await run('--json', 'search', 'x');
    expect(JSON.parse(out[0]!)).toEqual([
      { url: 'https://a/', title: 'A', description: 'about a' },
      { url: 'https://b/', title: 'B', description: '' },
    ]);
    fakeBackend(() => ({ json: { results: [] } }));
    await run('search', 'x');
    expect(out.at(-1)).toBe('No results found.');
  });
});

describe('fetch, screenshot, pdf, execute-js, crawl', () => {
  it('fetch -f raw prints markdown', async () => {
    const { out } = capture();
    const calls = fakeBackend((c) => (c.path === '/fetch' ? fetched(c.body.url) : { json: { markdown: '# T' } }));
    await run('fetch', 'https://e.com/', '-f', 'raw');
    expect(calls[1]!.body.filter).toBe('raw');
    expect(out).toEqual(['# T']);
  });

  it('fetch rejects an unknown filter', async () => {
    const { err } = capture();
    await expect(run('fetch', 'https://e.com/', '-f', 'bm25')).rejects.toEqual(new Exit(1));
    expect(err[0]).toMatch(/^invalid_params/);
  });

  it('screenshot -w seconds becomes the capture wait and prints base64', async () => {
    const { out } = capture();
    const calls = fakeBackend(() => ({ json: { status: 200, url: 'https://e.com/', mode: 'fast', b64: 'PNG64' } }));
    await run('screenshot', 'https://e.com/', '-w', '3');
    expect(calls[0]!.body.wait_ms).toBe(3000);
    expect(out).toEqual(['PNG64']);
  });

  it('pdf prints base64', async () => {
    const { out } = capture();
    fakeBackend(() => ({ json: { status: 200, url: 'https://e.com/', mode: 'fast', b64: 'PDF64' } }));
    await run('pdf', 'https://e.com/');
    expect(out).toEqual(['PDF64']);
  });

  it('execute-js collects repeated --script and needs at least one', async () => {
    const { out, err } = capture();
    const calls = fakeBackend(() => ({ json: { status: 200, url: 'https://e.com/', mode: 'fast', results: [1, 2] } }));
    await run('execute-js', 'https://e.com/', '-s', '1', '--script', '2');
    expect(calls[0]!.body.scripts).toEqual(['1', '2']);
    expect(JSON.parse(out[0]!).results).toEqual([1, 2]);
    await expect(run('execute-js', 'https://e.com/')).rejects.toEqual(new Exit(1));
    expect(err).toEqual(['At least one --script is required']);
  });

  it('crawl passes the selector and per-URL timeout', async () => {
    const { out } = capture();
    const calls = fakeBackend((c) => (c.path === '/fetch' ? fetched(c.body.url) : { json: { markdown: 'm' } }));
    await run('crawl', 'https://a.com/', 'https://b.com/', '--selector', 'main', '--timeout', '5000');
    const fetches = calls.filter((c) => c.path === '/fetch');
    expect(fetches.map((c) => [c.body.url, c.body.timeout_ms])).toEqual([['https://a.com/', 5000], ['https://b.com/', 5000]]);
    expect(calls.find((c) => c.path === '/markdown')!.body.css_selector).toBe('main');
    expect(JSON.parse(out[0]!).results).toHaveLength(2);
  });
});

describe('wayback', () => {
  const cdx = [
    ['timestamp', 'original', 'mimetype', 'statuscode', 'digest', 'length'],
    ['20200102030405', 'https://e.com/', 'text/html', '200', 'D', '1'],
  ];
  const raw = (body: string) => ({ json: { status: 200, url: 'https://web.archive.org/x', body, size: body.length, mode: 'stealth' } });

  it('snapshots passes the range, limit and match type, and pretty-prints', async () => {
    const { out } = capture();
    const calls = fakeBackend(() => raw(JSON.stringify(cdx)));
    await run('snapshots', 'e.com', '--from', '2020', '--to', '2021', '-l', '3', '--match', 'prefix');
    const q = new URL(calls[0]!.body.url).searchParams;
    expect([q.get('from'), q.get('to'), q.get('limit'), q.get('matchType')]).toEqual(['2020', '2021', '3', 'prefix']);
    expect(out).toEqual([
      'Found 1 snapshot(s):\n',
      '  2020-01-02 03:04:05  200  text/html',
      '  https://web.archive.org/web/20200102030405/https://e.com/\n',
    ]);
  });

  it('snapshots --json, and none found', async () => {
    const { out } = capture();
    fakeBackend(() => raw(JSON.stringify(cdx)));
    await run('--json', 'snapshots', 'e.com');
    expect(JSON.parse(out[0]!)[0].timestamp).toBe('20200102030405');
    fakeBackend(() => raw('[]'));
    await run('snapshots', 'e.com');
    expect(out.at(-1)).toBe('No snapshots found.');
  });

  it('archive requires --timestamp', async () => {
    const { err } = capture();
    await expect(run('archive', 'e.com')).rejects.toEqual(new Exit(1));
    expect(err).toEqual(['--timestamp is required']);
  });

  it('archive --original fetches the id_ capture; pretty and --json output', async () => {
    const { out } = capture();
    const calls = fakeBackend((c) => (c.path === '/raw' ? raw('<p>old</p>') : { json: { markdown: 'old' } }));
    await run('archive', 'e.com', '-t', '2020', '--original');
    expect(calls[0]!.body.url).toBe('https://web.archive.org/web/id_2020/e.com');
    expect(out).toEqual(['Wayback URL: https://web.archive.org/web/id_2020/e.com', 'Content length: 3 characters\n', 'old']);
    await run('--json', 'archive', 'e.com', '-t', '2020');
    expect(JSON.parse(out.at(-1)!)).toEqual({ waybackUrl: 'https://web.archive.org/web/2020/e.com', contentLength: 3, content: 'old' });
  });
});

describe('index.ts', () => {
  it('parses process.argv and runs the command', async () => {
    const { out } = capture();
    fakeBackend(() => ({ json: { results: [{ url: 'https://a/', title: 'A', content: '' }] } }));
    const argv = process.argv;
    process.argv = ['node', 'web-tools', '--json', 'search', 'x'];
    try {
      await import('../src/index.js');
      await vi.waitFor(() => expect(out).toHaveLength(1));
    } finally {
      process.argv = argv;
    }
    expect(JSON.parse(out[0]!)).toEqual([{ url: 'https://a/', title: 'A', description: '' }]);
  });
});

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { runRest } from '../src/handler.js';
import { createServer } from '../src/mcp.js';

/** Answer every sidecar/SearXNG call with `reply(path)`. */
function stubFetch(reply: (path: string) => unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: URL | string) => new Response(JSON.stringify(reply(new URL(String(input)).pathname)))),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('REST validates with the MCP schemas', () => {
  it('rejects a missing required field with 400 and the zod issues', async () => {
    const r = await runRest('web_fetch', {});
    expect(r.status).toBe(400);
    expect(r.body).toMatchObject({ error: 'invalid_params', issues: [{ path: ['url'] }] });
  });

  it('rejects a timeout above the sidecar cap instead of capping it silently', async () => {
    const r = await runRest('web_html', { url: 'https://example.com/', timeout_ms: 180_000 });
    expect(r.status).toBe(400);
    expect(r.body).toMatchObject({ issues: [{ path: ['timeout_ms'], code: 'too_big' }] });
  });

  it('rejects wrong types and enum values', async () => {
    expect((await runRest('web_fetch', { url: 'https://example.com/', f: 'markdown' })).status).toBe(400);
    expect((await runRest('web_search', { query: 'x', limit: '5' })).status).toBe(400);
    expect((await runRest('web_crawl', { urls: Array(21).fill('https://example.com/') })).status).toBe(400);
  });

  it('404s an unknown tool', async () => {
    expect(await runRest('web_nope', {})).toMatchObject({ status: 404 });
  });

  it('does not call the backend when validation fails', async () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    await runRest('web_html', { url: 'not a url' });
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe('REST v0 bodies stay backward compatible', () => {
  it('JSON-native tools answer with the bare payload', async () => {
    stubFetch(() => ({ results: [{ url: 'https://a/', title: 'A', content: 'a' }] }));
    const r = await runRest('web_search', { query: 'x' });
    expect(r).toEqual({ status: 200, body: [{ url: 'https://a/', title: 'A', description: 'a' }] });
  });

  it('JSON-native tool failures are HTTP 500 {error}', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('down', { status: 502 })));
    const r = await runRest('web_search', { query: 'x' });
    expect(r.status).toBe(500);
    expect(r.body).toMatchObject({ error: expect.stringContaining('searxng HTTP 502') });
  });

  it('screenshots come back as base64 text content, as before', async () => {
    stubFetch(() => ({ status: 200, url: 'https://example.com/', mode: 'fast', b64: 'iVBORw==' }));
    const r = await runRest('web_screenshot', { url: 'https://example.com/' });
    expect(r).toEqual({ status: 200, body: { content: [{ type: 'text', text: 'iVBORw==' }], isError: false } });
  });

  it('PDFs come back as base64 text content, as before', async () => {
    stubFetch(() => ({ status: 200, url: 'https://example.com/', mode: 'fast', b64: 'JVBERi0x' }));
    const r = await runRest('web_pdf', { url: 'https://example.com/' });
    expect(r).toEqual({ status: 200, body: { content: [{ type: 'text', text: 'JVBERi0x' }], isError: false } });
  });

  it('non-data tool failures stay HTTP 200 with isError (the v0 contract)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('down', { status: 502 })));
    const r = await runRest('web_html', { url: 'https://example.com/' });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ isError: true, content: [{ type: 'text', text: expect.stringMatching(/^web_html error: scrapling: .*; camoufox: /) }] });
  });
});

describe('MCP', () => {
  async function connect() {
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await createServer().connect(serverSide);
    const client = new Client({ name: 'test', version: '0' });
    await client.connect(clientSide);
    return client;
  }

  it('returns screenshots as image content and PDFs as an embedded resource', async () => {
    const client = await connect();
    stubFetch((path) => ({ status: 200, url: 'https://example.com/', mode: 'fast', b64: path === '/pdf' ? 'JVBERi0x' : 'iVBORw==' }));
    const shot = await client.callTool({ name: 'web_screenshot', arguments: { url: 'https://example.com/' } });
    expect(shot.content).toEqual([{ type: 'image', data: 'iVBORw==', mimeType: 'image/png' }]);
    const pdf = await client.callTool({ name: 'web_pdf', arguments: { url: 'https://example.com/' } });
    expect(pdf.content).toEqual([
      { type: 'resource', resource: { uri: 'https://example.com/', mimeType: 'application/pdf', blob: 'JVBERi0x' } },
    ]);
  });

  it('sends JSON-native results as JSON text, without the REST-only data field', async () => {
    const client = await connect();
    stubFetch(() => ({ results: [{ url: 'https://a/', title: 'A', content: 'a' }] }));
    const r = await client.callTool({ name: 'web_search', arguments: { query: 'x' } });
    expect(r).not.toHaveProperty('data');
    expect(JSON.parse((r.content as Array<{ text: string }>)[0]!.text)).toEqual([
      { url: 'https://a/', title: 'A', description: 'a' },
    ]);
  });

  it('lists every tool with its annotations', async () => {
    const client = await connect();
    const { tools } = await client.listTools();
    expect(tools).toHaveLength(17);
    expect(tools.find((t) => t.name === 'web_recycle')?.annotations?.destructiveHint).toBe(true);
  });
});

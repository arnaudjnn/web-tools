// The real Express server (src/index.ts) on a loopback port: auth, routes,
// MCP over HTTP, and the SIGTERM drain. Sidecars are faked at global fetch;
// loopback requests pass through to the real fetch.
import { createServer as netServer, Socket } from 'node:net';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { TOOL_NAMES } from '@web-tools/toolkit';

const realFetch = globalThis.fetch;
let base = '';
let port = 0;
let sigterm: () => void;
let exit: ReturnType<typeof vi.spyOn>;

// What the fake sidecars answer; the drain test swaps in a slow one.
let sidecar: (path: string) => Promise<Response> = async (path) =>
  new Response(
    JSON.stringify(
      path === '/search'
        ? { results: [{ url: 'https://a/', title: 'A', content: 'a' }] }
        : { status: 200, url: 'https://example.com/', mode: 'fast', b64: 'iVBORw==', html: '<p>x</p>', size: 8, escalated: false },
    ),
  );

async function freePort(): Promise<number> {
  const s = netServer();
  await new Promise<void>((r) => s.listen(0, '127.0.0.1', r));
  const p = (s.address() as { port: number }).port;
  await new Promise((r) => s.close(r));
  return p;
}

const call = (path: string, init: RequestInit = {}) => realFetch(`${base}${path}`, init);
const post = (path: string, body: unknown, headers: Record<string, string> = {}) =>
  call(path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
const AUTH = { Authorization: 'Bearer test-key' };

beforeAll(async () => {
  port = await freePort();
  base = `http://127.0.0.1:${port}`;
  process.env.PORT = String(port);
  process.env.DRAIN_TIMEOUT_MS = '400';
  exit = vi.spyOn(process, 'exit').mockImplementation((() => undefined) as never);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: URL | string | Request, init?: RequestInit) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      if (url.hostname === '127.0.0.1') return realFetch(input, init);
      return sidecar(url.pathname);
    }),
  );
  const before = new Set(process.listeners('SIGTERM'));
  await import('../src/index.js');
  sigterm = process.listeners('SIGTERM').find((l) => !before.has(l)) as () => void;
  for (let i = 0; i < 50; i++) {
    try {
      if ((await call('/health')).ok) break;
    } catch {
      await new Promise((r) => setTimeout(r, 20));
    }
  }
});

afterAll(() => {
  vi.unstubAllGlobals();
  exit.mockRestore();
});

describe('auth', () => {
  it('/health needs no key', async () => {
    const r = await call('/health');
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ status: 'ok' });
  });

  it('accepts the Bearer header (any case of "Bearer")', async () => {
    expect((await post('/api/v0/web_search', { query: 'x' }, AUTH)).status).toBe(200);
    expect((await post('/api/v0/web_search', { query: 'x' }, { Authorization: 'bearer test-key' })).status).toBe(200);
  });

  it('accepts ?api_key=', async () => {
    const r = await post('/api/v0/web_search?api_key=test-key', { query: 'x' });
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual([{ url: 'https://a/', title: 'A', description: 'a' }]);
  });

  it('does NOT accept X-API-Key', async () => {
    const r = await post('/api/v0/web_search', { query: 'x' }, { 'X-API-Key': 'test-key' });
    expect(r.status).toBe(403);
    expect(await r.json()).toEqual({ error: 'forbidden', error_description: 'Invalid or missing API key' });
  });

  it('rejects a missing, empty, wrong or wrong-length key, on every route', async () => {
    for (const headers of [{}, { Authorization: 'Bearer ' }, { Authorization: 'Bearer test-kez' }, { Authorization: 'Bearer test-key-longer' }]) {
      expect((await post('/api/v0/web_search', { query: 'x' }, headers)).status).toBe(403);
    }
    expect((await call('/stats')).status).toBe(403);
    expect((await call('/api/v0')).status).toBe(403);
    expect((await post('/mcp', {})).status).toBe(403);
    expect((await call('/api/v0?api_key=nope')).status).toBe(403);
  });
});

describe('REST routes', () => {
  it('GET /api/v0 lists every tool', async () => {
    const { tools } = (await (await call('/api/v0', { headers: AUTH })).json()) as { tools: Array<{ name: string; description: string }> };
    expect(tools.map((t) => t.name).sort()).toEqual([...TOOL_NAMES].sort());
    expect(tools.every((t) => t.description.length > 0)).toBe(true);
  });

  it('400 {error, issues} on invalid params, through the JSON body parser', async () => {
    const r = await post('/api/v0/web_fetch', { url: 'nope' }, AUTH);
    expect(r.status).toBe(400);
    expect(await r.json()).toMatchObject({ error: 'invalid_params', issues: [{ path: ['url'] }] });
  });

  it('an unknown tool has no route', async () => {
    expect((await post('/api/v0/web_nope', {}, AUTH)).status).toBe(404);
  });

  it('non-data tools answer {content, isError}', async () => {
    const r = await post('/api/v0/web_screenshot', { url: 'https://example.com/' }, AUTH);
    expect(await r.json()).toEqual({ content: [{ type: 'text', text: 'iVBORw==' }], isError: false });
  });

  it('GET /stats returns the counters', async () => {
    const s = (await (await call('/stats', { headers: AUTH })).json()) as { total_calls: number; by_tool: Record<string, unknown> };
    expect(s.total_calls).toBeGreaterThan(0);
    expect(Object.keys(s.by_tool).sort()).toEqual([...TOOL_NAMES].sort());
  });

  it('GET and DELETE /mcp are 405 (stateless server, no SSE stream)', async () => {
    for (const method of ['GET', 'DELETE']) {
      const r = await call('/mcp', { method, headers: AUTH });
      expect(r.status).toBe(405);
      expect(await r.json()).toMatchObject({ jsonrpc: '2.0', error: { code: -32000 } });
    }
  });
});

describe('MCP over streamable HTTP', () => {
  async function connect() {
    const transport = new StreamableHTTPClientTransport(new URL(`${base}/mcp`), { requestInit: { headers: AUTH } });
    const client = new Client({ name: 'test', version: '0' });
    await client.connect(transport);
    return client;
  }

  it('registers every tool by name', async () => {
    const client = await connect();
    const { tools } = await client.listTools();
    expect(tools).toHaveLength(TOOL_NAMES.length);
    expect(tools.map((t) => t.name).sort()).toEqual([...TOOL_NAMES].sort());
    expect(tools.every((t) => t.inputSchema.type === 'object')).toBe(true);
    await client.close();
  });

  it('calls a tool and returns MCP image content', async () => {
    const client = await connect();
    const r = await client.callTool({ name: 'web_screenshot', arguments: { url: 'https://example.com/' } });
    expect(r).toMatchObject({ content: [{ type: 'image', data: 'iVBORw==', mimeType: 'image/png' }], isError: false });
    await client.close();
  });

  it('rejects invalid arguments with the shared schema', async () => {
    const client = await connect();
    const r = await client.callTool({ name: 'web_fetch', arguments: { url: 'nope' } });
    expect(r.isError).toBe(true);
    expect((r.content as Array<{ text: string }>)[0]!.text).toMatch(/-32602: Input validation error: .*web_fetch[\s\S]*"url"/);
    await client.close();
  });
});

// Last: it closes the server.
describe('SIGTERM drain', () => {
  it('stops accepting, finishes in-flight calls, answers /health 503, and is bounded by DRAIN_TIMEOUT_MS', async () => {
    let release!: () => void;
    let started!: () => void;
    const inFlight = new Promise<void>((r) => (started = r));
    sidecar = async () => {
      started();
      await new Promise<void>((r) => (release = r));
      return new Response(JSON.stringify({ status: 200, url: 'https://example.com/', html: '<p>slow</p>', size: 11, mode: 'fast', escalated: false }));
    };

    // A raw keep-alive socket, so a second request can ride the in-flight connection.
    const sock = new Socket();
    let raw = '';
    sock.on('data', (d) => (raw += d.toString()));
    const ended = new Promise<void>((r) => sock.on('close', () => r()));
    await new Promise<void>((r) => sock.connect(port, '127.0.0.1', r));
    const body = JSON.stringify({ url: 'https://example.com/' });
    sock.write(
      `POST /api/v0/web_html HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer test-key\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\n\r\n${body}`,
    );
    await inFlight;

    sigterm();
    sigterm(); // idempotent

    // New connections are refused once draining.
    await expect(call('/health')).rejects.toThrow();
    // An existing connection is told the instance is draining.
    sock.write('GET /health HTTP/1.1\r\nHost: t\r\n\r\n');

    await new Promise((r) => setTimeout(r, 100));
    expect(exit).not.toHaveBeenCalled();

    // The deadline fires while the call is still in flight.
    await vi.waitFor(() => expect(exit).toHaveBeenCalledWith(1), { timeout: 2000 });

    // The in-flight call still completes, then the server closes and exits 0.
    release();
    await ended;
    const responses = raw.split(/(?=HTTP\/1\.1 )/);
    expect(responses).toHaveLength(2);
    expect(responses[0]).toMatch(/^HTTP\/1\.1 200 /);
    expect(responses[0]).toContain('slow');
    expect(responses[1]).toMatch(/^HTTP\/1\.1 503 /);
    expect(responses[1]).toMatch(/connection: close/i);
    expect(responses[1]).toContain('{"status":"draining"}');
    await vi.waitFor(() => expect(exit).toHaveBeenLastCalledWith(0), { timeout: 2000 });
  });
});

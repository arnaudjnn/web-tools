// A fake for the two sidecars and SearXNG behind the global fetch.
import { vi } from 'vitest';
import { camoufox } from '../src/camoufox.js';
import { scrapling } from '../src/scrapling.js';
import { resetStats } from '../src/stats.js';

type Reply = { status?: number; json?: unknown; text?: string } | 'refused' | 'timeout';
export type Handler = (path: string, body: any) => Reply;

export type Call = { host: string; path: string; body: any };

export function refused(): Error {
  return Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
}

/** Route fetches by host (scrapling.test / camoufox.test / searxng.test). */
export function fakeSidecars(handlers: Partial<Record<'scrapling' | 'camoufox' | 'searxng', Handler>>) {
  const calls: Call[] = [];
  scrapling.reset();
  camoufox.reset();
  resetStats();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: URL | string, init?: RequestInit) => {
      const url = new URL(String(input));
      const host = url.hostname.split('.')[0] as 'scrapling' | 'camoufox' | 'searxng';
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ host, path: url.pathname, body });
      const handler = handlers[host];
      const reply = handler ? handler(url.pathname, body) : 'refused';
      if (reply === 'refused') throw refused();
      if (reply === 'timeout') throw Object.assign(new Error('The operation was aborted due to timeout'), { name: 'TimeoutError' });
      const payload = reply.json !== undefined ? JSON.stringify(reply.json) : (reply.text ?? '');
      return new Response(payload, { status: reply.status ?? 200, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return calls;
}

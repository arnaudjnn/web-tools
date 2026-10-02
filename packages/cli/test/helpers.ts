// Shared CLI test plumbing: a fake backend behind global fetch, captured
// stdout/stderr, and process.exit turned into a catchable throw.
import { Command } from 'commander';
import { vi } from 'vitest';
import { registerCrawlCommand } from '../src/commands/crawl.js';
import { registerFetchCommand } from '../src/commands/fetch.js';
import { registerSearchCommand } from '../src/commands/search.js';
import { registerWaybackCommand } from '../src/commands/wayback.js';

export class Exit extends Error {
  constructor(readonly code: number | undefined) {
    super(`exit ${code}`);
  }
}

export type Call = { host: string; path: string; url: URL; body: any };

/** Answer by host (scrapling.test / camoufox.test / searxng.test) and path. */
export function fakeBackend(reply: (c: Call) => { status?: number; json?: unknown; text?: string }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: URL | string, init?: RequestInit) => {
      const url = new URL(String(input));
      const c = { host: url.hostname.split('.')[0]!, path: url.pathname, url, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(c);
      const r = reply(c);
      return new Response(r.json !== undefined ? JSON.stringify(r.json) : (r.text ?? ''), { status: r.status ?? 200 });
    }),
  );
  return calls;
}

export function capture() {
  const out: string[] = [];
  const err: string[] = [];
  vi.spyOn(console, 'log').mockImplementation((...a: unknown[]) => void out.push(a.map(String).join(' ')));
  vi.spyOn(console, 'error').mockImplementation((...a: unknown[]) => void err.push(a.map(String).join(' ')));
  vi.spyOn(process, 'exit').mockImplementation(((code?: number) => {
    throw new Exit(code);
  }) as never);
  return { out, err };
}

/** The same program index.ts builds, without parsing process.argv. */
export function program(): Command {
  const p = new Command().name('web-tools').option('--json', 'Output raw JSON').exitOverride();
  registerSearchCommand(p);
  registerFetchCommand(p);
  registerCrawlCommand(p);
  registerWaybackCommand(p);
  return p;
}

export const run = (...args: string[]) => program().parseAsync(['node', 'web-tools', ...args]);

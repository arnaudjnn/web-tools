// The shared sidecar client: deadlines, the circuit breaker, and the error
// status/detail every caller (routing fallback, form outcomes) depends on.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { camoufoxFormInspect, camoufoxFormSubmit, camoufoxRender } from '../src/camoufox.js';
import { scraplingFetch } from '../src/scrapling.js';
import { BREAKER_COOLDOWN_MS, createSidecar, isUnreachable, SidecarError } from '../src/sidecar.js';
import { fakeSidecars, refused } from './sidecars.js';

class TestError extends SidecarError {}

function client(url: string | undefined = 'http://side.test:1') {
  const onTrip = vi.fn();
  const c = createSidecar({ name: 'side', url: () => url, error: (m, s, d) => new TestError(m, s, d), onTrip });
  return { c, onTrip };
}

const stub = (impl: (url: string, init: RequestInit) => Promise<Response>) => {
  const f = vi.fn(async (input: URL | string, init?: RequestInit) => impl(String(input), init ?? {}));
  vi.stubGlobal('fetch', f);
  return f;
};

const caught = async (p: Promise<unknown>): Promise<TestError> => {
  try {
    await p;
  } catch (err) {
    return err as TestError;
  }
  throw new Error('expected a rejection');
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('createSidecar post', () => {
  it('POSTs JSON to the path on the base URL and returns the parsed body', async () => {
    const f = stub(async () => new Response(JSON.stringify({ ok: 1 })));
    const { c } = client();
    expect(await c.post('/x', { a: 1 }, 5000)).toEqual({ ok: 1 });
    const [url, init] = f.mock.calls[0]!;
    expect(String(url)).toBe('http://side.test:1/x');
    expect(init).toMatchObject({ method: 'POST', body: '{"a":1}', headers: { 'Content-Type': 'application/json' } });
    expect(init!.signal).toBeInstanceOf(AbortSignal);
  });

  it('refuses to run without a configured URL', async () => {
    const f = stub(async () => new Response('{}'));
    const err = await caught(client('').c.post('/x', {}, 1000));
    expect(err).toBeInstanceOf(TestError);
    expect(err.message).toBe('SIDE_URL is not configured');
    expect(f).not.toHaveBeenCalled();
  });

  it('carries the HTTP status and the FastAPI detail on an error', async () => {
    stub(async () => new Response(JSON.stringify({ detail: { message: 'busy', retryable: true } }), { status: 503 }));
    const err = await caught(client().c.post('/x', {}, 1000));
    expect(err).toBeInstanceOf(TestError);
    expect(err.status).toBe(503);
    expect(err.detail).toEqual({ message: 'busy', retryable: true });
    expect(err.message).toMatch(/^side \/x HTTP 503: \{"detail"/);
  });

  it('detail is the whole JSON body when there is no `detail`, undefined for non-JSON; text is capped', async () => {
    stub(async () => new Response(JSON.stringify({ error: 'x' }), { status: 500 }));
    expect((await caught(client().c.post('/x', {}, 1000))).detail).toEqual({ error: 'x' });

    stub(async () => new Response('<html>' + 'y'.repeat(1000), { status: 502 }));
    const err = await caught(client().c.post('/x', {}, 1000));
    expect(err.status).toBe(502);
    expect(err.detail).toBeUndefined();
    expect(err.message.length).toBeLessThan(350);
  });

  it('an HTTP error does not trip the breaker', async () => {
    stub(async () => new Response('no', { status: 500 }));
    const { c, onTrip } = client();
    await caught(c.post('/x', {}, 1000));
    expect(c.available()).toBe(true);
    expect(onTrip).not.toHaveBeenCalled();
  });

  it('a timeout does not trip the breaker (a slow sidecar is present)', async () => {
    stub(async () => {
      throw Object.assign(new Error('The operation was aborted due to timeout'), { name: 'TimeoutError' });
    });
    const { c, onTrip } = client();
    const err = await caught(c.post('/x', {}, 1000));
    expect(err.message).toMatch(/aborted due to timeout/);
    expect(err.status).toBeUndefined();
    expect(c.available()).toBe(true);
    expect(onTrip).not.toHaveBeenCalled();
  });
});

describe('circuit breaker', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-01T00:00:00Z'));
  });

  it('opens on unreachable, fails fast while open, and reports the trip once', async () => {
    const f = stub(async () => {
      throw refused();
    });
    const { c, onTrip } = client();
    const first = await caught(c.post('/x', {}, 1000));
    expect(first.message).toBe('side /x unreachable: fetch failed (ECONNREFUSED)');
    expect(first.status).toBeUndefined();
    expect(c.available()).toBe(false);
    expect(onTrip).toHaveBeenCalledTimes(1);
    expect(onTrip).toHaveBeenCalledWith('fetch failed (ECONNREFUSED)');

    const skipped = await caught(c.post('/y', {}, 1000));
    expect(skipped.message).toBe(`side /y skipped: breaker open (unreachable within the last ${BREAKER_COOLDOWN_MS / 1000}s)`);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('half-open after the cooldown: one probe; success keeps it closed', async () => {
    let up = false;
    const f = stub(async () => {
      if (!up) throw refused();
      return new Response('{"ok":true}');
    });
    const { c } = client();
    await caught(c.post('/x', {}, 1000));
    vi.setSystemTime(Date.now() + BREAKER_COOLDOWN_MS - 1);
    expect(c.available()).toBe(false);
    vi.setSystemTime(Date.now() + 1);
    expect(c.available()).toBe(true);
    up = true;
    expect(await c.post('/x', {}, 1000)).toEqual({ ok: true });
    expect(await c.post('/x', {}, 1000)).toEqual({ ok: true });
    expect(f).toHaveBeenCalledTimes(3);
  });

  it('half-open after the cooldown: a failed probe re-opens it and reports again', async () => {
    stub(async () => {
      throw refused();
    });
    const { c, onTrip } = client();
    await caught(c.post('/x', {}, 1000));
    vi.setSystemTime(Date.now() + BREAKER_COOLDOWN_MS);
    await caught(c.post('/x', {}, 1000));
    expect(c.available()).toBe(false);
    expect(onTrip).toHaveBeenCalledTimes(2);
  });

  it('reset() closes it', async () => {
    stub(async () => {
      throw refused();
    });
    const { c } = client();
    await caught(c.post('/x', {}, 1000));
    c.reset();
    expect(c.available()).toBe(true);
  });
});

describe('isUnreachable', () => {
  it('matches the no-such-service codes anywhere in a short cause chain', () => {
    for (const code of ['ENOTFOUND', 'ECONNREFUSED', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH', 'ERR_INVALID_URL']) {
      expect(isUnreachable({ code })).toBe(true);
    }
    expect(isUnreachable(new TypeError('fetch failed', { cause: { cause: { code: 'ENOTFOUND' } } }))).toBe(true);
  });

  it('does not match busy/slow failures, or a code buried too deep', () => {
    expect(isUnreachable({ code: 'ECONNRESET' })).toBe(false);
    expect(isUnreachable({ name: 'TimeoutError' })).toBe(false);
    expect(isUnreachable(undefined)).toBe(false);
    let deep: unknown = { code: 'ECONNREFUSED' };
    for (let i = 0; i < 5; i++) deep = { cause: deep };
    expect(isUnreachable(deep)).toBe(false);
  });
});

describe('client deadlines sit above the sidecar deadline', () => {
  it('scrapling +25 s, camoufox +30 s, forms +60 s', async () => {
    fakeSidecars({ scrapling: () => ({ json: {} }), camoufox: () => ({ json: {} }), camoufoxForms: () => ({ json: {} }) });
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    await scraplingFetch({ url: 'https://e.com/', timeoutMs: 10_000 });
    await camoufoxRender({ url: 'https://e.com/', timeoutMs: 10_000 });
    await camoufoxFormSubmit({ url: 'https://e.com/', fields: [], submit: 'b', timeoutMs: 10_000 });
    await camoufoxFormInspect({ url: 'https://e.com/', timeoutMs: 10_000 });
    expect(timeout.mock.calls.map((c) => c[0])).toEqual([35_000, 40_000, 70_000, 70_000]);
  });
});

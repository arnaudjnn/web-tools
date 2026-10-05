// The one HTTP client for both sidecars (Scrapling, Camoufox): JSON POST, a
// client deadline, error bodies surfaced, and a circuit breaker.
//
// The breaker trips ONLY on "there is no such service" — DNS failure or a
// refused/unroutable connection, which fail in milliseconds — never on a
// timeout or an HTTP error. A slow or busy sidecar is present and working;
// tripping on that once demoted LinkedIn to the datacenter IP for minutes.
// While open, calls fail immediately so the router falls through to the other
// sidecar (or the local markdown renderer) without paying a dial per call.
// The cooldown is short on purpose: every minute open is a minute degraded.
//
// No retries here, ever. A form submission behind a lost response may still
// have happened, so retrying belongs to callers that know their request is
// idempotent — and none of the current ones need it.

import './http.js';

export class SidecarError extends Error {
  constructor(
    message: string,
    /** HTTP status when the sidecar answered; undefined for transport failures. */
    readonly status?: number,
    /** The sidecar's parsed FastAPI `detail` (e.g. `{message, retryable}`), else the parsed body. */
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

function parseDetail(text: string): unknown {
  try {
    const parsed = JSON.parse(text) as unknown;
    return parsed && typeof parsed === 'object' && 'detail' in parsed ? (parsed as { detail: unknown }).detail : parsed;
  } catch {
    return undefined;
  }
}

const UNREACHABLE_CODES = new Set([
  'ENOTFOUND',
  'ECONNREFUSED',
  'EAI_AGAIN',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'ERR_INVALID_URL',
]);

/** Does this error mean "no such service" (as opposed to busy or slow)? */
export function isUnreachable(err: unknown): boolean {
  let cur: unknown = err;
  for (let depth = 0; cur && depth < 5; depth++) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === 'string' && UNREACHABLE_CODES.has(code)) return true;
    cur = (cur as { cause?: unknown }).cause;
  }
  return false;
}

export const BREAKER_COOLDOWN_MS = 60_000;

// Replica spreading. Railway's private DNS returns one AAAA per replica, but
// fetch resolves once and keeps reusing that connection, so every call landed
// on the FIRST replica (2026-10-05: 4 parallel forms on 2 replicas queued on
// one; the other sat idle). A spreading client resolves the host itself and
// sends each call to the replica with the fewest calls in flight.
const SPREAD_DNS_TTL_MS = 30_000;
const spreadCache = new Map<string, { at: number; ips: string[] }>();
const inFlight = new Map<string, number>();

async function replicaIps(host: string): Promise<string[]> {
  const hit = spreadCache.get(host);
  if (hit && Date.now() - hit.at < SPREAD_DNS_TTL_MS) return hit.ips;
  try {
    const { resolve6 } = await import('node:dns/promises');
    const ips = (await resolve6(host)).sort();
    spreadCache.set(host, { at: Date.now(), ips });
    return ips;
  } catch {
    return [];
  }
}

/** The URL to call and a release(); falls back to the base URL when the host
 *  does not resolve to several replicas (local dev, a single replica). */
export async function pickReplica(base: string): Promise<{ url: string; release: () => void }> {
  const parsed = new URL(base);
  const ips = parsed.hostname.endsWith('.railway.internal') ? await replicaIps(parsed.hostname) : [];
  if (ips.length < 2) return { url: base, release: () => {} };
  const ip = ips.reduce((best, cur) => ((inFlight.get(cur) ?? 0) < (inFlight.get(best) ?? 0) ? cur : best));
  inFlight.set(ip, (inFlight.get(ip) ?? 0) + 1);
  parsed.hostname = `[${ip}]`;
  let released = false;
  return {
    url: parsed.toString(),
    release: () => {
      if (released) return;
      released = true;
      inFlight.set(ip, Math.max(0, (inFlight.get(ip) ?? 1) - 1));
    },
  };
}

/** Tests only. */
export function resetSpread(): void {
  spreadCache.clear();
  inFlight.clear();
}

export type SidecarClient = {
  readonly name: string;
  /** POST JSON; `clientTimeoutMs` is the socket deadline, not the sidecar's own. */
  post<T>(path: string, body: unknown, clientTimeoutMs: number): Promise<T>;
  /** False while the breaker is open. */
  available(): boolean;
  /** Close the breaker (tests). */
  reset(): void;
};

export function createSidecar(opts: {
  name: string;
  url: () => string | undefined;
  /** Build the sidecar's own error class, so callers can still `instanceof`. */
  error: (message: string, status?: number, detail?: unknown) => SidecarError;
  onTrip?: (reason: string) => void;
  /** Spread calls across the host's replicas (pickReplica). */
  spread?: boolean;
}): SidecarClient {
  const { name } = opts;
  let openUntil = 0;
  const available = () => Date.now() >= openUntil;

  return {
    name,
    available,
    reset: () => {
      openUntil = 0;
    },
    async post<T>(path: string, body: unknown, clientTimeoutMs: number): Promise<T> {
      const base = opts.url();
      if (!base) throw opts.error(`${name.toUpperCase()}_URL is not configured`);
      if (!available()) {
        throw opts.error(`${name} ${path} skipped: breaker open (unreachable within the last ${BREAKER_COOLDOWN_MS / 1000}s)`);
      }

      const target = opts.spread ? await pickReplica(base) : { url: base, release: () => {} };
      let response: Response;
      try {
        response = await fetch(new URL(path, target.url), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(clientTimeoutMs),
        });
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err);
        const cause = (err as { cause?: { code?: string } }).cause?.code;
        if (isUnreachable(err)) {
          const firstTrip = available();
          openUntil = Date.now() + BREAKER_COOLDOWN_MS;
          if (firstTrip) opts.onTrip?.(cause ? `${reason} (${cause})` : reason);
        }
        target.release();
        throw opts.error(`${name} ${path} unreachable: ${reason}${cause ? ` (${cause})` : ''}`);
      }

      try {
        if (!response.ok) {
          const text = await response.text().catch(() => '');
          throw opts.error(`${name} ${path} HTTP ${response.status}: ${text.slice(0, 300)}`, response.status, parseDetail(text));
        }
        return (await response.json()) as T;
      } finally {
        target.release();
      }
    },
  };
}

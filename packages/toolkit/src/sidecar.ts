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

      let response: Response;
      try {
        response = await fetch(new URL(path, base), {
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
        throw opts.error(`${name} ${path} unreachable: ${reason}${cause ? ` (${cause})` : ''}`);
      }

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        throw opts.error(`${name} ${path} HTTP ${response.status}: ${text.slice(0, 300)}`, response.status, parseDetail(text));
      }
      return (await response.json()) as T;
    },
  };
}

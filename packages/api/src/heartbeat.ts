import type { Response } from 'express';

/**
 * Keep long calls alive through Railway's public edge, which closes a request
 * after 5 minutes with no bytes transferred (15 min while bytes flow).
 * web_form_submit with score_gate + retry_on_captcha_rejection legitimately
 * runs 300–540 s; without a heartbeat the client got an empty-body error at
 * ~300 s while the sidecar was still working.
 *
 * HEARTBEAT_MS (default 20 s) is both the grace period before a REST call
 * commits its response and the interval between heartbeat bytes.
 */
export const heartbeatMs = (): number => {
  const n = Number(process.env.HEARTBEAT_MS);
  return Number.isFinite(n) && n > 0 ? n : 20_000;
};

let active = 0;
/** Heartbeat timers currently running (REST + MCP). Tests assert it drains. */
export const activeHeartbeats = (): number => active;

/** Run `beat` every `ms` (first after `ms`) until the response closes or stop() is called. */
function every(res: Response, ms: number, beat: () => void): () => void {
  let stopped = false;
  active++;
  const timer = setInterval(() => {
    if (res.writableEnded || res.destroyed) return stop();
    try {
      beat();
    } catch {
      stop(); // a write on a dying socket: never crash the process over a heartbeat
    }
  }, ms);
  function stop() {
    if (stopped) return;
    stopped = true;
    active--;
    clearInterval(timer);
    res.off('close', stop);
  }
  res.on('close', stop);
  return stop;
}

export type RestOutcome = { status: number; body: unknown };

/**
 * REST: answer exactly as before when `work` finishes within `ms`. Past that,
 * commit 200 application/json, write a space every `ms` (leading whitespace is
 * valid JSON), then the body. The body already carries the outcome — `{error}`
 * for JSON-native tools, `{content, isError}` for the rest — because the HTTP
 * status can no longer change.
 */
export async function respondWithHeartbeat(
  res: Response,
  work: () => Promise<RestOutcome>,
  ms: number = heartbeatMs(),
): Promise<void> {
  let committed = false;
  const stop = every(res, ms, () => {
    if (!committed) {
      committed = true;
      res.status(200);
      res.set({ 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' });
      res.flushHeaders();
    }
    res.write(' ');
  });

  let outcome: RestOutcome;
  try {
    outcome = await work();
  } catch (err) {
    stop();
    if (!committed) throw err; // fast path: Express's error handler, as before
    outcome = { status: 500, body: { error: err instanceof Error ? err.message : String(err) } };
  }
  stop();

  if (res.destroyed || res.writableEnded) return; // client went away; the result has nowhere to go
  if (!committed) {
    res.status(outcome.status).json(outcome.body);
    return;
  }
  res.end(JSON.stringify(outcome.body));
}

/**
 * MCP: the SDK's streamable HTTP transport answers a POSTed request with an SSE
 * stream (enableJsonResponse is off) and exposes no hook to write on it, so we
 * write SSE comment lines on the Node response between its events. Comments
 * are ignored by every SSE parser (the SDK client uses eventsource-parser), and
 * the SDK writes each event as a single chunk, so a ping never splits one.
 */
export function sseHeartbeat(res: Response, ms: number = heartbeatMs()): () => void {
  return every(res, ms, () => {
    if (!res.headersSent || res.statusCode !== 200) return;
    if (!String(res.getHeader('content-type') ?? '').startsWith('text/event-stream')) return;
    res.write(': ping\n\n');
  });
}

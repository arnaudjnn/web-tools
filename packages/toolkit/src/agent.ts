// web_agent — a natural-language task drives a stealth Chromium (browser-use)
// on the Scrapling sidecar's POST /agent. See services/scrapling/agent.py.
//
// Self-contained on purpose: the sidecar answers two statuses that are data,
// not failures — 503 {disabled, reason} when no LLM key is configured, and
// 429 {busy, retryable} when the replica's single agent slot is taken — and
// the shared Scrapling client folds every non-2xx into one error string.
// This client reads them as statuses instead.
//
// Deadlines line up with the sidecar's: the runner stops itself at timeout_ms
// and returns a partial result, the sidecar kills it at timeout_ms + 15 s, and
// this client aborts at timeout_ms + 25 s — so the sidecar's honest answer
// always wins the race.

import { Config } from './config.js';
import { recordCall } from './stats.js';
import type { ToolResult } from './types.js';

export type AgentStep = { n: number; action: string; url: string; error?: string };

export type AgentResult = {
  final_result: unknown;
  success: boolean;
  steps: AgentStep[];
  urls: string[];
  duration_s: number;
  blocked_requests: { reason: 'mutation' | 'domain'; method: string; type: string; url: string }[];
  form_submissions: Record<string, unknown>[];
  allowed_domains: string[];
  stealth: boolean;
  error: string | null;
};

export type AgentOutcome =
  | { kind: 'ok'; result: AgentResult }
  | { kind: 'disabled'; reason: string }
  | { kind: 'busy'; reason: string };

export const AGENT_DEFAULT_TIMEOUT_MS = 180_000;
const CLIENT_SLACK_MS = 25_000;

export class AgentError extends Error {}

export async function scraplingAgent(params: {
  task: string;
  startUrl?: string;
  maxSteps?: number;
  allowedDomains?: string[];
  outputSchema?: Record<string, unknown>;
  timeoutMs?: number;
  stealth?: boolean;
  allowMutations?: boolean;
  allowFormSubmit?: boolean;
}): Promise<AgentOutcome> {
  if (!Config.scrapling.url) throw new AgentError('SCRAPLING_URL is not configured');
  const timeoutMs = params.timeoutMs ?? AGENT_DEFAULT_TIMEOUT_MS;
  const body = {
    task: params.task,
    ...(params.startUrl ? { start_url: params.startUrl } : {}),
    ...(params.maxSteps !== undefined ? { max_steps: params.maxSteps } : {}),
    ...(params.allowedDomains ? { allowed_domains: params.allowedDomains } : {}),
    ...(params.outputSchema ? { output_schema: params.outputSchema } : {}),
    timeout_ms: timeoutMs,
    ...(params.stealth ? { stealth: true } : {}),
    ...(params.allowMutations ? { allow_mutations: true } : {}),
    ...(params.allowFormSubmit ? { allow_form_submit: true } : {}),
  };

  let response: Response;
  try {
    response = await fetch(new URL('/agent', Config.scrapling.url), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs + CLIENT_SLACK_MS),
    });
  } catch (err) {
    throw new AgentError(`scrapling /agent failed: ${err instanceof Error ? err.message : String(err)}`);
  }

  const text = await response.text().catch(() => '');
  let json: Record<string, unknown> = {};
  try {
    json = JSON.parse(text) as Record<string, unknown>;
  } catch {
    // non-JSON error body; fall through with the raw text
  }
  if (response.status === 503 && json.disabled === true) {
    return { kind: 'disabled', reason: String(json.reason ?? 'set AGENT_LLM_API_KEY') };
  }
  if (response.status === 429 && json.busy === true) {
    return { kind: 'busy', reason: String(json.reason ?? 'agent busy') };
  }
  if (response.status === 404) {
    // A sidecar built before /agent existed. Same message as "disabled", so a
    // caller does not have to tell the two apart.
    return { kind: 'disabled', reason: 'this Scrapling sidecar has no /agent endpoint (redeploy it)' };
  }
  if (!response.ok) {
    const detail = typeof json.detail === 'string' ? json.detail : text.slice(0, 300);
    throw new AgentError(`scrapling /agent HTTP ${response.status}: ${detail}`);
  }
  return { kind: 'ok', result: json as unknown as AgentResult };
}

// ── Tool handler ──────────────────────────────────────────────────────

function done(result: ToolResult): ToolResult {
  recordCall('web_agent', result.content?.[0]?.text?.length ?? 0, !!result.isError);
  return result;
}

/**
 * Run a browser-use agent. `disabled` and `busy` come back as errors with a
 * stable prefix (the caller cannot proceed either way), never as a crash. An
 * agent that ran but did not finish is NOT an error: the partial result —
 * steps, urls, error 'max_steps…' / 'deadline…' — is the useful answer.
 */
export async function web_agent(params: Record<string, unknown>): Promise<ToolResult> {
  const task = typeof params.task === 'string' ? params.task : '';
  if (!task) {
    return { content: [{ type: 'text', text: 'web_agent error: `task` is required' }], isError: true };
  }
  const startUrl = typeof params.start_url === 'string' ? params.start_url : undefined;
  const allowedDomains = Array.isArray(params.allowed_domains)
    ? (params.allowed_domains as unknown[]).filter((d): d is string => typeof d === 'string')
    : undefined;
  if (!startUrl && !allowedDomains?.length) {
    return {
      content: [{ type: 'text', text: 'web_agent error: `start_url` or `allowed_domains` is required' }],
      isError: true,
    };
  }
  try {
    const r = await scraplingAgent({
      task,
      startUrl,
      allowedDomains,
      maxSteps: typeof params.max_steps === 'number' ? params.max_steps : undefined,
      outputSchema:
        params.output_schema && typeof params.output_schema === 'object'
          ? (params.output_schema as Record<string, unknown>)
          : undefined,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
      stealth: params.stealth === true,
      allowMutations: params.allow_mutations === true,
      allowFormSubmit: params.allow_form_submit === true,
    });
    if (r.kind === 'disabled') {
      return done({ content: [{ type: 'text', text: `web_agent disabled: ${r.reason}` }], isError: true });
    }
    if (r.kind === 'busy') {
      return done({ content: [{ type: 'text', text: `web_agent busy (retryable): ${r.reason}` }], isError: true });
    }
    return done({ content: [{ type: 'text', text: JSON.stringify(r.result) }], isError: false });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    process.stderr.write(`web_agent failed: ${msg}\n`);
    return done({ content: [{ type: 'text', text: `web_agent error: ${msg}` }], isError: true });
  }
}

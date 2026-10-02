// web_agent: the sidecar's 503 disabled / 429 busy are statuses, not crashes;
// a run that did not finish is a result, not an error.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentError, scraplingAgent } from '../src/agent.js';
import { functionMap } from '../src/functions.js';
import { getStats } from '../src/stats.js';
import { fakeSidecars } from './sidecars.js';

const RESULT = {
  final_result: { price: 3 },
  success: false,
  steps: [{ n: 1, action: 'go_to_url', url: 'https://example.com/' }],
  urls: ['https://example.com/'],
  duration_s: 4.2,
  blocked_requests: [],
  form_submissions: [],
  allowed_domains: ['example.com'],
  stealth: false,
  error: 'max_steps reached',
};

const textOf = (r: { content: Array<{ type: string; text?: string }> }) => r.content[0]!.text!;

afterEach(() => vi.unstubAllGlobals());

describe('web_agent statuses', () => {
  it('busy (429) is a retryable isError result, not a counted crash', async () => {
    fakeSidecars({ scrapling: () => ({ status: 429, json: { busy: true, retryable: true, reason: 'one run per replica' } }) });
    const r = await functionMap.web_agent({ task: 't', start_url: 'https://example.com/' });
    expect(r).toEqual({ content: [{ type: 'text', text: 'web_agent busy (retryable): one run per replica' }], isError: true });
    expect(getStats().by_tool.web_agent).toMatchObject({ calls: 1, errors: 1 });
  });

  it('disabled without a reason falls back to the documented hint', async () => {
    fakeSidecars({ scrapling: () => ({ status: 503, json: { disabled: true } }) });
    const r = await functionMap.web_agent({ task: 't', start_url: 'https://example.com/' });
    expect(textOf(r)).toBe('web_agent disabled: set AGENT_LLM_API_KEY');
  });

  it('a sidecar without /agent (404) reads as disabled', async () => {
    fakeSidecars({ scrapling: () => ({ status: 404, json: { detail: 'Not Found' } }) });
    expect(textOf(await functionMap.web_agent({ task: 't', start_url: 'https://example.com/' }))).toMatch(
      /^web_agent disabled: this Scrapling sidecar has no \/agent endpoint/,
    );
  });

  it('a 503 that is not {disabled} or a 429 that is not {busy} is a plain HTTP error', async () => {
    fakeSidecars({ scrapling: () => ({ status: 503, json: { detail: 'browser not ready' } }) });
    expect(textOf(await functionMap.web_agent({ task: 't', start_url: 'https://example.com/' }))).toBe(
      'web_agent error: scrapling /agent HTTP 503: browser not ready',
    );
    fakeSidecars({ scrapling: () => ({ status: 429, text: 'rate limited' }) });
    expect(textOf(await functionMap.web_agent({ task: 't', start_url: 'https://example.com/' }))).toBe(
      'web_agent error: scrapling /agent HTTP 429: rate limited',
    );
  });

  it('a non-JSON 500 body is surfaced raw', async () => {
    fakeSidecars({ scrapling: () => ({ status: 500, text: 'Internal Server Error' }) });
    await expect(scraplingAgent({ task: 't', startUrl: 'https://example.com/' })).rejects.toThrow(
      new AgentError('scrapling /agent HTTP 500: Internal Server Error'),
    );
  });

  it('an unfinished run is the useful answer, not an error', async () => {
    fakeSidecars({ scrapling: () => ({ json: RESULT }) });
    const r = await functionMap.web_agent({ task: 't', start_url: 'https://example.com/' });
    expect(r.isError).toBe(false);
    expect(JSON.parse(textOf(r))).toEqual(RESULT);
    expect(getStats().by_tool.web_agent.errors).toBe(0);
  });
});

describe('web_agent request', () => {
  it('forwards every option in the sidecar wire format, with the default deadline', async () => {
    const calls = fakeSidecars({ scrapling: () => ({ json: RESULT }) });
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    await functionMap.web_agent({
      task: 'find the price',
      start_url: 'https://example.com/',
      allowed_domains: ['example.com', 7, 'cdn.example.com'],
      max_steps: 5,
      output_schema: { type: 'object' },
      stealth: true,
      allow_mutations: true,
      allow_form_submit: true,
    });
    expect(calls[0]).toEqual({
      host: 'scrapling',
      path: '/agent',
      body: {
        task: 'find the price',
        start_url: 'https://example.com/',
        allowed_domains: ['example.com', 'cdn.example.com'],
        max_steps: 5,
        output_schema: { type: 'object' },
        timeout_ms: 180_000,
        stealth: true,
        allow_mutations: true,
        allow_form_submit: true,
      },
    });
    // The client aborts 25 s after the sidecar's own deadline.
    expect(timeout).toHaveBeenCalledWith(205_000);
    timeout.mockRestore();
  });

  it('sends only what was asked (flags are opt-in)', async () => {
    const calls = fakeSidecars({ scrapling: () => ({ json: RESULT }) });
    await functionMap.web_agent({ task: 't', allowed_domains: ['example.com'], timeout_ms: 60_000, stealth: 'yes' });
    expect(calls[0]!.body).toEqual({ task: 't', allowed_domains: ['example.com'], timeout_ms: 60_000 });
  });

  it('refuses a missing task, or a run with no domain fence, without calling the sidecar', async () => {
    const calls = fakeSidecars({ scrapling: () => ({ json: RESULT }) });
    expect(textOf(await functionMap.web_agent({ start_url: 'https://example.com/' }))).toBe('web_agent error: `task` is required');
    const unfenced = await functionMap.web_agent({ task: 't', allowed_domains: [] });
    expect(unfenced).toMatchObject({ isError: true });
    expect(textOf(unfenced)).toBe('web_agent error: `start_url` or `allowed_domains` is required');
    expect(calls).toHaveLength(0);
  });
});

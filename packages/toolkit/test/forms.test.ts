// web_form_submit outcome mapping (never retried) and web_form_inspect passthrough.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { functionMap } from '../src/functions.js';
import { getStats } from '../src/stats.js';
import { fakeSidecars, type Handler } from './sidecars.js';

const FORM = { url: 'https://www.example.it/f', fields: [{ selector: '#a', value: 'x' }], submit: 'button' };

const bodyOf = (r: { content: Array<{ type: string; text?: string }> }) => JSON.parse(r.content[0]!.text!);

afterEach(() => vi.unstubAllGlobals());

async function submitWith(camoufox: Handler, params: Record<string, unknown> = FORM) {
  const calls = fakeSidecars({ camoufoxForms: camoufox, scrapling: () => ({ json: {} }) });
  const r = await functionMap.web_form_submit(params);
  return { r, body: bodyOf(r), calls };
}

describe('web_form_submit outcomes', () => {
  it('an answered run is passed through, never marked retryable', async () => {
    const answer = { contract_version: 2, ok: true, form_submissions: 1, status: 200, url: 'https://www.example.it/ok', error: null, retryable: true };
    const { r, body } = await submitWith(() => ({ json: answer }));
    expect(r.isError).toBe(false);
    expect(body).toEqual({ ...answer, retryable: false });
  });

  it('503 + retryable → not_submitted, form_submissions 0', async () => {
    const { body } = await submitWith(() => ({ status: 503, json: { detail: { message: 'warming', retryable: true } } }));
    expect(body).toEqual({ ok: false, retryable: true, outcome: 'not_submitted', form_submissions: 0, status: 503, error: 'warming' });
  });

  it('503 without retryable:true is unknown (a POST may have left)', async () => {
    const { body } = await submitWith(() => ({ status: 503, json: { detail: { message: 'died mid-run' } } }));
    expect(body).toEqual({ ok: false, retryable: false, outcome: 'unknown', form_submissions: null, status: 503, error: 'died mid-run' });
  });

  it('400 → invalid_request with the detail string', async () => {
    const { body } = await submitWith(() => ({ status: 400, json: { detail: 'submit selector matched nothing' } }));
    expect(body).toEqual({
      ok: false, retryable: false, outcome: 'invalid_request', form_submissions: 0, status: 400, error: 'submit selector matched nothing',
    });
  });

  it('422 → invalid_request carrying the validation detail as JSON', async () => {
    const detail = [{ loc: ['body', 'fields', 0, 'selector'], msg: 'Field required' }];
    const { body } = await submitWith(() => ({ status: 422, json: { detail } }));
    expect(body).toMatchObject({ outcome: 'invalid_request', retryable: false, form_submissions: 0, status: 422 });
    expect(JSON.parse(body.error)).toEqual(detail);
  });

  it('502, a non-JSON error, and a lost response are unknown', async () => {
    expect((await submitWith(() => ({ status: 502, json: { detail: { message: 'nav crashed' } } }))).body).toMatchObject({
      outcome: 'unknown', status: 502, error: 'nav crashed', form_submissions: null,
    });
    const html = (await submitWith(() => ({ status: 504, text: '<html>gateway</html>' }))).body;
    expect(html).toMatchObject({ outcome: 'unknown', status: 504 });
    expect(html.error).toMatch(/HTTP 504: <html>gateway/);
    expect((await submitWith(() => 'timeout')).body).toMatchObject({ outcome: 'unknown', status: 0, retryable: false });
  });

  it('is never retried and never falls back to scrapling, whatever the failure', async () => {
    for (const reply of ['refused', 'timeout', { status: 502, text: 'x' }, { status: 503, json: { detail: { retryable: true } } }] as const) {
      const { calls, body } = await submitWith(() => reply);
      expect(calls.map((c) => `${c.host}${c.path}`)).toEqual(['camoufox-forms/form-submit']);
      expect(body.ok).toBe(false);
    }
  });

  it('failures are counted', async () => {
    await submitWith(() => ({ status: 502, text: 'x' }));
    expect(getStats().by_tool.web_form_submit).toMatchObject({ calls: 1, errors: 1 });
  });

  it('refuses a call without url/fields/submit before any browser runs', async () => {
    const { r, body, calls } = await submitWith(() => ({ json: {} }), { url: FORM.url, fields: 'nope', submit: 'b' });
    expect(r.isError).toBe(true);
    expect(body).toMatchObject({ outcome: 'invalid_request', form_submissions: 0, retryable: false });
    expect(calls).toHaveLength(0);
  });

  it('maps every option to the sidecar wire format; fresh_ip defaults on', async () => {
    const { calls } = await submitWith(() => ({ json: { ok: true } }), {
      ...FORM,
      dismiss: ['#cookie'],
      success_url: 'grazie',
      submission_urls: ['https://api.example.it/lead'],
      captcha_field: 'g-recaptcha-response',
      require_captcha_token: true,
      ready_expression: 'window.ready',
      inspect_only: true,
      wait_until: 'networkidle',
      wait_ms: 100,
      settle_ms: 200,
      timeout_ms: 30_000,
      exit_session: 'abc',
      headed: true,
      gate_text: 'continua',
      step2: [{ selector: '#b', value: 'y' }],
      step2_submit: '#next',
      completion_markers: ['Grazie'],
      profile: 'warm-1',
      stop_after_posts: 2,
    });
    expect(calls[0]!.body).toEqual({
      url: FORM.url,
      fields: FORM.fields,
      submit: 'button',
      dismiss: ['#cookie'],
      success_url: 'grazie',
      submission_urls: ['https://api.example.it/lead'],
      captcha_field: 'g-recaptcha-response',
      require_captcha_token: true,
      ready_expression: 'window.ready',
      inspect_only: true,
      wait_until: 'networkidle',
      wait_ms: 100,
      settle_ms: 200,
      timeout_ms: 30_000,
      fresh_ip: true,
      exit_session: 'abc',
      headed: true,
      gate_text: 'continua',
      step2: [{ selector: '#b', value: 'y' }],
      step2_submit: '#next',
      completion_markers: ['Grazie'],
      profile: 'warm-1',
      stop_after_posts: 2,
    });
  });

  it('passes retry_on_captcha_rejection, captcha_rejection_text and score_threshold through', async () => {
    const { calls } = await submitWith(() => ({ json: { ok: true } }), {
      ...FORM,
      retry_on_captcha_rejection: 2,
      captcha_rejection_text: 'verifica non riuscita',
      score_threshold: 0.8,
      timeout_ms: 540_000,
    });
    expect(calls[0]!.body).toMatchObject({
      retry_on_captcha_rejection: 2,
      captcha_rejection_text: 'verifica non riuscita',
      score_threshold: 0.8,
      timeout_ms: 540_000,
    });
    // 0 is the default: not sent.
    const plain = await submitWith(() => ({ json: { ok: true } }), { ...FORM, retry_on_captcha_rejection: 0 });
    expect(plain.calls[0]!.body).not.toHaveProperty('retry_on_captcha_rejection');
  });

  it('a retried run carries attempts[] and the total form_submissions', async () => {
    const answer = {
      contract_version: 2, ok: true, form_submissions: 2, status: 302, url: 'https://www.example.it/done', error: null,
      attempts: [
        { n: 1, error: 'wizard_rejected', ok: false, status: 200, form_submissions: 1, score_gate_score: 0.8, asn: 3269 },
        { n: 2, error: null, ok: true, status: 302, form_submissions: 1, score_gate_score: 0.9, asn: 1267 },
      ],
    };
    const { body } = await submitWith(() => ({ json: answer }), { ...FORM, retry_on_captcha_rejection: 1 });
    expect(body).toEqual({ ...answer, retryable: false });
    // Without retries the sidecar's attempts:null never reaches the caller.
    const single = await submitWith(() => ({ json: { ...answer, attempts: null } }));
    expect(single.body).not.toHaveProperty('attempts');
  });

  it('an unknown retry keeps the known attempts but never a total', async () => {
    const attempts = [{ n: 1, error: 'wizard_rejected', form_submissions: 1 }, { n: 2, error: 'outcome_unknown', form_submissions: null }];
    const { body } = await submitWith(() => ({
      status: 502,
      json: { detail: { message: 'Form retry outcome unavailable', retryable: false, attempts, form_submissions_before: 1 } },
    }));
    expect(body).toEqual({
      ok: false, retryable: false, outcome: 'unknown', form_submissions: null, status: 502,
      error: 'Form retry outcome unavailable', attempts, form_submissions_before: 1,
    });
  });

  it('sends the minimal body by default; fresh_ip:false is honoured', async () => {
    const { calls } = await submitWith(() => ({ json: { ok: true } }), { ...FORM, fresh_ip: false, wait_ms: 'soon' });
    expect(calls[0]!.body).toEqual({ url: FORM.url, fields: FORM.fields, submit: 'button', timeout_ms: 120_000, fresh_ip: false });
  });
});

describe('web_form_inspect', () => {
  const INSPECT = {
    ok: true, error: null, form_submissions: 0, status: 200, url: 'https://www.example.it/f',
    forms: [{ index: 0, fields: [{ selector: '#a' }] }], captcha: [], cookie_banners: [], wizard: {}, diagnostics: {}, exit_session: 's',
  };

  it('passes the sidecar answer through unchanged', async () => {
    const calls = fakeSidecars({ camoufoxForms: () => ({ json: INSPECT }) });
    const r = await functionMap.web_form_inspect({ url: INSPECT.url });
    expect(r.isError).toBe(false);
    expect(bodyOf(r)).toEqual(INSPECT);
    expect(calls).toEqual([
      { host: 'camoufox-forms', path: '/form-inspect', body: { url: INSPECT.url, timeout_ms: 60_000, fresh_ip: true } },
    ]);
  });

  it('forwards its options', async () => {
    const calls = fakeSidecars({ camoufoxForms: () => ({ json: INSPECT }) });
    await functionMap.web_form_inspect({
      url: INSPECT.url, wait_until: 'load', wait_ms: 10, timeout_ms: 5000, fresh_ip: false, exit_session: 'x', headed: true, profile: 'p',
    });
    expect(calls[0]!.body).toEqual({
      url: INSPECT.url, wait_until: 'load', wait_ms: 10, timeout_ms: 5000, fresh_ip: false, exit_session: 'x', headed: true, profile: 'p',
    });
  });

  it('a bad request is not retryable; a transport failure is (it is read-only)', async () => {
    fakeSidecars({ camoufoxForms: () => ({ status: 422, json: { detail: 'bad url' } }) });
    expect(bodyOf(await functionMap.web_form_inspect({ url: INSPECT.url }))).toEqual({
      ok: false, retryable: false, form_submissions: 0, status: 422, error: 'bad url',
    });
    fakeSidecars({});
    const down = bodyOf(await functionMap.web_form_inspect({ url: INSPECT.url }));
    expect(down).toMatchObject({ ok: false, retryable: true, form_submissions: 0, status: 0 });
    expect(down.error).toMatch(/camoufox-forms \/form-inspect unreachable/);
  });

  it('requires url', async () => {
    const calls = fakeSidecars({});
    const r = await functionMap.web_form_inspect({});
    expect(r.isError).toBe(true);
    expect(bodyOf(r)).toEqual({ ok: false, retryable: false, error: '`url` is required' });
    expect(calls).toHaveLength(0);
  });
});

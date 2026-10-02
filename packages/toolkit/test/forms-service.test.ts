// The dedicated forms service (CAMOUFOX_FORMS_URL) and the score gate's wire.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Config, formsUrl } from '../src/config.js';
import { functionMap } from '../src/functions.js';
import { WebFormSubmitInput } from '../src/schemas.js';
import { fakeSidecars } from './sidecars.js';

const FORM = { url: 'https://www.example.it/f', fields: [{ selector: '#a', value: 'x' }], submit: 'button' };
const bodyOf = (r: { content: Array<{ type: string; text?: string }> }) => JSON.parse(r.content[0]!.text!);
const ORACLE = 'https://tools.test/oracle/recaptcha';

afterEach(() => {
  vi.unstubAllGlobals();
  (Config as { oracleUrl: string | null }).oracleUrl = null;
});

describe('CAMOUFOX_FORMS_URL', () => {
  it('falls back to CAMOUFOX_URL when unset or blank', () => {
    expect(formsUrl({ CAMOUFOX_URL: 'http://c:8000' })).toBe('http://c:8000');
    expect(formsUrl({ CAMOUFOX_URL: 'http://c:8000', CAMOUFOX_FORMS_URL: '  ' })).toBe('http://c:8000');
    expect(formsUrl({ CAMOUFOX_URL: 'http://c:8000', CAMOUFOX_FORMS_URL: 'http://f:8000' })).toBe('http://f:8000');
  });

  it('every form tool goes to the forms service; readers stay on Camoufox', async () => {
    const calls = fakeSidecars({
      camoufox: () => ({ json: { status: 200, url: 'u', html: '<p/>', b64: '', result: null } }),
      camoufoxForms: () => ({ json: { ok: true, form_submissions: 0 } }),
    });
    (Config as { oracleUrl: string | null }).oracleUrl = ORACLE;
    await functionMap.web_form_submit(FORM);
    await functionMap.web_form_inspect({ url: FORM.url });
    await functionMap.web_form_score_probe({});
    await functionMap.web_form_warm({ profile: 'p' });
    await functionMap.web_form_exit_select({ profile: 'p' });
    await functionMap.web_form_exits({});
    await functionMap.web_bytes({ url: 'https://www.example.it/a.pdf' });
    const where = calls.map((c) => `${c.host}${c.path}`);
    expect(where).toEqual([
      'camoufox-forms/form-submit',
      'camoufox-forms/form-inspect',
      'camoufox-forms/form-score-probe',
      'camoufox-forms/form-warm',
      'camoufox-forms/form-exit-select',
      'camoufox-forms/form-exits',
      'camoufox/bytes',
    ]);
  });

  it('a forms outage does not trip the readers', async () => {
    fakeSidecars({ camoufox: () => ({ json: { status: 200, b64: '' } }) }); // forms: refused
    expect(bodyOf(await functionMap.web_form_submit(FORM))).toMatchObject({ outcome: 'unknown' });
    const r = await functionMap.web_bytes({ url: 'https://www.example.it/a.pdf' });
    expect(r.isError).toBeFalsy();
  });
});

describe('score gate wire', () => {
  it('forwards score_gate options and our oracle', async () => {
    (Config as { oracleUrl: string | null }).oracleUrl = ORACLE;
    const calls = fakeSidecars({ camoufoxForms: () => ({ json: { ok: true } }) });
    await functionMap.web_form_submit({ ...FORM, headed: true, score_gate: true, score_threshold: 0.8, score_gate_tries: 2, timeout_ms: 360_000 });
    expect(calls[0]!.body).toMatchObject({
      headed: true, score_gate: true, score_threshold: 0.8, score_gate_tries: 2, oracle_url: ORACLE, timeout_ms: 360_000,
    });
  });

  it('score_gate:false sends no oracle (the sidecar cannot gate)', async () => {
    (Config as { oracleUrl: string | null }).oracleUrl = ORACLE;
    const calls = fakeSidecars({ camoufoxForms: () => ({ json: { ok: true } }) });
    await functionMap.web_form_submit({ ...FORM, headed: true, score_gate: false });
    expect(calls[0]!.body.score_gate).toBe(false);
    expect(calls[0]!.body.oracle_url).toBeUndefined();
  });

  it('no_scoring_exit is a retryable zero-POST failure carrying the gate record', async () => {
    const gate = { passed: false, tries: 3, probed: 2, skipped: ['asn_low_scores'], scores: [0.3, 0.5], chosen_score: null, asn: null };
    fakeSidecars({
      camoufoxForms: () => ({
        status: 503,
        json: { detail: { message: 'No exit scored', retryable: true, error: 'no_scoring_exit', form_submissions: 0, score_gate: gate } },
      }),
    });
    const body = bodyOf(await functionMap.web_form_submit({ ...FORM, headed: true }));
    expect(body).toEqual({
      ok: false, retryable: true, outcome: 'not_submitted', form_submissions: 0, status: 503,
      error: 'no_scoring_exit', score_gate: gate,
    });
  });

  it('the schema allows a gated wizard deadline (360 s) and bounds the gate', () => {
    expect(WebFormSubmitInput.safeParse({ ...FORM, timeout_ms: 360_000 }).success).toBe(true);
    expect(WebFormSubmitInput.safeParse({ ...FORM, timeout_ms: 360_001 }).success).toBe(false);
    expect(WebFormSubmitInput.safeParse({ ...FORM, score_gate_tries: 7 }).success).toBe(false);
    expect(WebFormSubmitInput.safeParse({ ...FORM, score_threshold: 1.5 }).success).toBe(false);
  });
});

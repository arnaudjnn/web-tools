// DIAGNOSTIC ONLY. Used against the local fixture by compatibility.test.mjs.
// Never shipped in the image: v0.2.2 mislabels real form POSTs as GET in CDP.
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function waitFor(page, expression, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await page.evaluate(expression)) return;
    await pause(100);
  }
  throw new Error('Required page state did not become ready');
}

export function submissionGuard(postUrl) {
  const expected = new URL(postUrl);
  let attempts = 0;
  let blocked = 0;
  return {
    decide(request) {
      const actual = new URL(request.url);
      const match = request.method === 'POST' && actual.origin === expected.origin && actual.pathname === expected.pathname;
      if (!match) return 'continue';
      if (attempts) { blocked++; return 'abort'; }
      attempts++;
      return 'continue';
    },
    snapshot: () => ({ form_submissions: attempts, blocked_submissions: blocked }),
  };
}

export async function submitForm(cdp, page, params) {
  const guard = submissionGuard(params.post_url ?? params.url);
  let interceptError;
  let postStatus = 0;
  const postIds = new Set();
  cdp.on('Fetch.requestPaused', (event, sessionId) => {
    if (sessionId !== page.sessionId) return;
    const before = guard.snapshot().form_submissions;
    const decision = guard.decide(event.request);
    if (guard.snapshot().form_submissions > before && event.networkId) postIds.add(event.networkId);
    void page.send(decision === 'abort' ? 'Fetch.failRequest' : 'Fetch.continueRequest', {
      requestId: event.requestId,
      ...(decision === 'abort' ? { errorReason: 'BlockedByClient' } : {}),
    }).catch(() => { interceptError = new Error('Request interception failed; do not retry this submission'); });
  });
  cdp.on('Network.responseReceived', (event, sessionId) => {
    if (sessionId === page.sessionId && postIds.has(event.requestId)) postStatus = event.response.status;
  });
  // Restrict interception to the form endpoint. All matched requests must be
  // continued explicitly; a second POST is aborted, never retried.
  await page.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
  try {
    const nav = await page.send('Page.navigate', { url: params.url });
    if (nav.errorText) throw new Error('Navigation failed');
    await waitFor(page, 'document.readyState === "complete"', params.timeout_ms);
    for (const selector of params.dismiss ?? []) {
      await page.evaluate(`document.querySelector(${JSON.stringify(selector)})?.click()`);
    }
    for (const field of params.fields) {
      await waitFor(page, `!!document.querySelector(${JSON.stringify(field.selector)})`, params.timeout_ms);
      const filled = await page.evaluate(`(() => {
        const field = ${JSON.stringify(field)};
        const el = document.querySelector(field.selector);
        if (!el || el.disabled) return false;
        if (field.action === 'check') { if (!el.checked) el.click(); return el.checked === true; }
        if (field.action === 'select' && !Array.from(el.options ?? []).some(o => o.value === field.value)) return false;
        el.focus();
        el.value = field.value ?? '';
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return el.value === (field.value ?? '');
      })()`);
      if (!filled) throw new Error('A required form control could not be filled');
    }
    if (params.ready_js) await waitFor(page, params.ready_js, params.timeout_ms);
    const clicked = await page.evaluate(`(() => {
      const button = document.querySelector(${JSON.stringify(params.submit)});
      if (!button || button.disabled) return false;
      button.click();
      return true;
    })()`);
    if (!clicked) throw new Error('Submit button is unavailable');
    const success = new RegExp(params.success_url);
    const deadline = Date.now() + params.settle_ms;
    let state;
    while (Date.now() < deadline) {
      if (interceptError) throw interceptError;
      state = await page.evaluate('({url:location.href,html:document.documentElement.outerHTML,errors:!!document.querySelector(".error-msg,.invalid-feedback,.errorlist")})');
      if (guard.snapshot().form_submissions === 1 && (success.test(state.url) || state.errors)) break;
      await pause(100);
    }
    const counts = guard.snapshot();
    const ok = counts.form_submissions === 1 && !!state && success.test(state.url);
    return { ...counts, status: postStatus, url: state?.url ?? params.url, html: state?.html ?? '', ok, engine: 'obscura',
      ...(!ok && !state?.errors ? { error: 'Acceptance not confirmed; do not retry this email' } : {}) };
  } catch (error) {
    return { ...guard.snapshot(), status: postStatus, url: params.url, html: '', ok: false, engine: 'obscura', error: error.message };
  }
}

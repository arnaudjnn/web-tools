import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { Cdp } from './cdp.mjs';
import { submissionGuard, submitForm } from './compatibility-probe.mjs';

test('only one POST may leave for the form endpoint', () => {
  const guard = submissionGuard('https://example.com/signup/');
  assert.equal(guard.decide({ method: 'GET', url: 'https://example.com/signup/' }), 'continue');
  assert.equal(guard.decide({ method: 'POST', url: 'https://example.com/other' }), 'continue');
  assert.equal(guard.decide({ method: 'POST', url: 'https://example.com/signup/?step=1' }), 'continue');
  assert.equal(guard.decide({ method: 'POST', url: 'https://example.com/signup/' }), 'abort');
  assert.deepEqual(guard.snapshot(), { form_submissions: 1, blocked_submissions: 1 });
});

test('v0.2.2 regression: real form POST is invisible to the POST guard', {
  skip: !process.env.OBSCURA_TEST_CDP, timeout: 30_000,
}, async () => {
  const requests = [];
  const server = createServer(async (req, res) => {
    if (req.method === 'POST') {
      let body = '';
      for await (const chunk of req) body += chunk;
      requests.push(body);
      res.writeHead(303, { location: '/done' }).end();
      return;
    }
    res.setHeader('content-type', 'text/html');
    res.end(req.url === '/done' ? '<h1>Done</h1>' : `<!doctype html><html><body>
      <form method="POST" action="/submit"><input id="name" name="name">
      <input id="agree" name="agree" type="checkbox">
      <select id="country" name="country"><option value="IT">Italy</option></select>
      <button id="submit" type="submit">Send</button></form></body></html>`);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  let cdp;
  try {
    cdp = await Cdp.connect(process.env.OBSCURA_TEST_CDP);
    const page = await cdp.page();
    const base = `http://127.0.0.1:${server.address().port}`;
    const result = await submitForm(cdp, page, {
      url: base, post_url: `${base}/submit`, submit: '#submit', success_url: '/done$',
      timeout_ms: 5000, settle_ms: 5000,
      fields: [
        { selector: '#name', value: 'Test' },
        { selector: '#agree', action: 'check' },
        { selector: '#country', action: 'select', value: 'IT' },
      ],
    });
    // Deliberately pin the incompatibility. Remove the production form gate
    // only after a new engine passes a POSITIVE one-POST + duplicate test.
    assert.equal(result.ok, false, JSON.stringify(result));
    assert.equal(result.form_submissions, 0);
    assert.equal(requests.length, 1);
    const posted = new URLSearchParams(requests[0]);
    assert.equal(posted.get('name'), 'Test');
    assert.equal(posted.get('country'), 'IT');
    assert.equal(posted.get('agree'), 'on');
  } finally {
    cdp?.close();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

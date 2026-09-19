# Obscura evaluation service

Pinned to upstream **v0.2.2**, with SHA-256-verified Linux archives. A small
Node 22 HTTP/CDP adapter has no npm dependencies. Each operation gets a fresh
engine, one operation runs at a time, deadlines kill the child, and requests
never fall back to another browser. No stealth flag is enabled.

## Current gate: not a Camoufox signup replacement

On 2026-09-19, the real v0.2.2 engine passed basic navigation and JavaScript.
Its native form submission reached the fixture server as **POST**, but its
`Fetch.requestPaused` event reported **GET**. A method-based interception guard
therefore observed zero POSTs even while the server received one. Playwright
`selectOption` also threw a DOM implementation error.

`POST /form-submit` consequently returns **501 before opening any browser**.
`GET /healthz` explicitly reports `form_submission_supported: false`. Existing
Camoufox traffic and Scartoffie's signup backend are NOT switched to this
engine. A green deployment means render/eval readiness, not signup readiness.

The reproducer lives in `compatibility-probe.mjs` and `compatibility.test.mjs`;
neither is copied into the production image. It only submits to its local test
server. Before lifting the gate, a new pinned engine must pass positive tests
for correct POST metadata, duplicate-POST blocking, form controls, cookies,
redirects and interrupted/unknown outcomes. A target site's email eligibility
or CAPTCHA rejection is not fixed by changing the browser engine.

## API

- `GET /healthz`: public readiness and capability information.
- `POST /render`: `{ "url": "https://example.com", "timeout_ms": 30000 }`.
- `POST /eval`: same plus `"js": "document.title"`.
- `POST /form-submit`: deliberately unsupported (501, zero submissions).

Operations require `Authorization: Bearer <API_KEY>`. Requests are limited to
64 KiB, timeouts to 180 seconds, and concurrency to one (429 means no work
started). `/eval` runs the supplied JavaScript: do not use it to evade the form
capability gate. Engine-level file and private-network access remain disabled.
CDP binds only to loopback and is never exposed by Railway.

## Railway

Deploy a separate **Obscura** service in the **web-tools** project, root directory
`/services/obscura`, config path `/services/obscura/railway.json`, branch `main`.
Use narrow watch paths `/services/obscura/**`. Required variables:

```
PORT=8000
API_KEY=${{Tools.API_KEY}}
```

`PROXY_URL` is optional and static; no automatic rotation or retry is performed.
Leave it unset for the direct baseline. Do not publish a public CDP port.
The HTTP health check executes JavaScript in the actual engine at startup.

## Local verification

```
node --test services/obscura/server.test.mjs services/obscura/compatibility.test.mjs
```

To reproduce the engine incompatibility, start the **verified v0.2.2** binary
locally with `obscura serve --port 19222 --allow-private-network`, then:

```
OBSCURA_TEST_CDP=ws://127.0.0.1:19222/devtools/browser node --test services/obscura/compatibility.test.mjs
```

The private-network flag is only for that local HTTP fixture, never production.
For the HTTP service, set `OBSCURA_BINARY` to the downloaded binary path,
`API_KEY` to a development key and `PORT` to a free port, then run
`node services/obscura/server.mjs`.

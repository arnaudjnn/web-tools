# Deploy and Host Web Tools on Railway

Web Tools is an open-source web toolkit that gives AI agents fifteen tools to search, read, and act on the web (fetch, crawl, screenshot, archive, fill forms), available as an MCP server, REST API, and CLI. It consumes zero LLM tokens for web access, so your models spend their budget on reasoning, not searching.

## About Hosting Web Tools

This template deploys a complete self-hosted web toolkit as five services on Railway: **Redis** (cache), **SearXNG** (privacy-respecting metasearch engine), **Scrapling** (stealth fetching, rendering, screenshots, PDFs and JS execution, with residential egress and JS-challenge solving), **Camoufox** (stealth Firefox on a geo-targeted residential exit, for sources that refuse anything else), and the **Web Tools Server** that ties them together. An API key is auto-generated at deploy time to secure your endpoint. Once deployed, any MCP-compatible client (Claude Code, Claude Desktop, Cursor, Windsurf, etc.) can connect over HTTP, and the REST API (`POST /api/v0/{tool_name}`) serves non-MCP integrations. You own the infrastructure; the data never leaves your stack.

## Common Use Cases

- **Replace paid web APIs**: Open-source alternative to Firecrawl, Linkup, Tavily, Exa, Bright Data and Browser Use. Search, fetch, crawl and submit forms without per-query costs
- **Supercharge AI coding agents**: Connect Claude Code or Cursor to self-hosted web search and page fetching. Replace their built-in WebSearch and WebFetch tools so every search is private and free
- **Web research and monitoring**: Search the web, fetch pages as clean markdown, take screenshots, generate PDFs, execute JavaScript on pages, and query the Wayback Machine for historical snapshots

## Dependencies for Web Tools Hosting

- **Redis** (7-alpine): In-memory cache used by SearXNG for rate limiting and result caching
- **SearXNG**: Privacy-respecting metasearch engine that aggregates results from Google, Brave, DuckDuckGo, and more. Builds from `services/searxng/Dockerfile` with optional `PROXY_URL` support for outgoing requests
- **Scrapling**: Stealth fetch sidecar, and the whole render pipeline: `web_fetch`, `web_html`, `web_crawl` (as sequential markdown posts), `web_screenshot`, `web_pdf`, `web_execute_js`. Owns the rotating residential egress (for IP-reputation walls such as LinkedIn), the JS-challenge solving (for Cloudflare-style walls), and a hard per-request deadline with `busy_age_s` observable on `/healthz`. Builds from `services/scrapling/Dockerfile`; set `PROXY_URL` on it to enable `mode=stealth`
- **Camoufox**: Stealth Firefox sidecar on a **geo-targeted** residential exit, with a fingerprint whose locale and timezone derive from the exit IP. Serves the sources the other two cannot reach at all: ones that bot-gate datacenter IPs outright, or score the exit country as part of an anti-bot sensor decision. Also owns the two capabilities nothing else here has: a binary/PDF fetch through that exit (`web_bytes`) and warmed anti-bot sensor sessions (`web_spa_fetch`). Builds from `services/camoufox/Dockerfile`; set `PROXY_URL` (geo-targeted) and keep `WORKERS=1`
- **Web Tools Server** (Node.js 22): The HTTP server exposing MCP and REST API endpoints. Builds from the **repo-root `Dockerfile`**. Do not delete it; it is this service's build

### Deployment Dependencies

- [Web Tools GitHub Repository](https://github.com/arnaudjnn/web-tools)
- [SearXNG Documentation](https://docs.searxng.org/)
- [Model Context Protocol Specification](https://modelcontextprotocol.io/)

### Implementation Details

The Web Tools Server exposes two interfaces:

**MCP**: Streamable HTTP endpoint at `/mcp` for MCP clients:

```json
{
  "mcpServers": {
    "web_tools": {
      "type": "http",
      "url": "https://your-server.up.railway.app/mcp",
      "headers": {
        "Authorization": "Bearer your-api-key"
      }
    }
  }
}
```

**REST API**: Standard HTTP endpoints at `/api/v0/{tool_name}`:

```bash
curl -X POST https://your-server.up.railway.app/api/v0/web_search \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"query": "railway deployment"}'
```

The fifteen tools available are: `web_search`, `web_fetch`, `web_html`, `web_screenshot`, `web_pdf`, `web_execute_js`, `web_crawl`, `web_bytes`, `web_form_submit`, `web_eval`, `web_spa_fetch`, `web_recycle`, `web_snapshots`, `web_archive`, and `web_usage_stats`.

Callers never choose a fetch engine. Which of the two browsers serves a URL, and whether it egresses through a residential proxy, in what country, or solves a JS challenge, is decided from the host inside the server. Adding a knob for it would put the burden of knowing which engine can reach which site on every caller. When the preferred engine fails, the server falls back to the other one and reports both causes only when both fail.

#### Railway Service Configuration

| Service | Source | Root Directory | Notes |
| --- | --- | --- | --- |
| Web Tools Server | GitHub repo | *(repo root)* | Builds the root `Dockerfile`; exposes MCP + REST |
| SearXNG | GitHub repo | `services/searxng` | Optional `PROXY_URL` |
| Scrapling | GitHub repo | `services/scrapling` | `PROXY_URL` (US-geo), `PORT=8000` |
| Camoufox | GitHub repo | `services/camoufox` | `PROXY_URL` (target-geo), `PORT=8000`, `WORKERS=1` |
| Redis | Docker image | n/a | Used by SearXNG |

**Set Root Directory before connecting a subfolder service to the repo.** Railway
resolves a service's build config by walking up from its Root Directory, so a
subfolder service without one inherits the repo root's `Dockerfile`, which is the
Node server. The build then goes green and the container crashes on `ZodError: API_KEY
Required`, because it is running the wrong program; and it repeats on every push,
so a service deployed correctly by hand will replace itself later. `railway up`
does not fix it (it uploads the right files while the stored config still points at
`/`), and the CLI cannot set the field, so use the dashboard or the API mutation
documented in the README.

**Both stealth sidecars run 2 replicas**, and that is a throughput requirement
rather than redundancy. Each container serves one request per mode at a time (one
warmed session per mode, pinned to a single-slot executor), so concurrent callers
queue. Measured on one replica under load: an unrelated fetch waited 90.5s; 0.7s with
two.

**Scale the browsers by replicas, not workers.** Camoufox keeps `WORKERS=1`: a
warmed anti-bot session cannot be shared across processes. Use
`railway service scale --service camoufox eu-west=2`, and note `scale` ADDS to the
existing regions, so pass `us-east=0` to move rather than spread. Otherwise you get
replicas on two continents and a transatlantic round trip per request.

**When every browser launch 502s with `CanCreateUserNamespace() clone() failure:
EACCES`, restart the Camoufox service.** The Firefox sandbox needs user
namespaces, which the container sometimes loses (observed 2026-09-26: all of
/render, /eval, /spa-fetch and /form-submit failing at launch for hours, fixed
by a restart that cleared it). `railway restart --service Camoufox --yes`, then
confirm `/healthz` reports `render_browser_ok: true`. If a restart stops curing
it, the escape hatch is `MOZ_DISABLE_CONTENT_SANDBOX=1` on the Camoufox service —
deliberately not the default, because disabling the content sandbox weakens the
fingerprint this browser exists for.

**Give only the Web Tools Server a public domain.** It is the authenticated front
door; the other services talk over Railway's private network and should have no
domain at all. SearXNG in particular has no authentication of its own, so a public
domain makes it an open search proxy whose outgoing requests spend your metered
`PROXY_URL` bandwidth. `SEARXNG_SECRET_KEY` does not change that, because it is
an internal signing secret rather than a credential. Removing the domain is what
makes a service private; removing its credentials just makes it broken or open.

**Wire the services together with reference variables** rather than hardcoded
`*.railway.internal` hostnames, so the wiring survives a rename and each port is
only right in one place:

```
SCRAPLING_URL = http://${{Scrapling.RAILWAY_PRIVATE_DOMAIN}}:${{Scrapling.PORT}}
CAMOUFOX_URL  = http://${{Camoufox.RAILWAY_PRIVATE_DOMAIN}}:${{Camoufox.PORT}}
SEARXNG_URL   = http://${{SearXNG.RAILWAY_PRIVATE_DOMAIN}}:8080
```

Reference `${{Service.PORT}}` only where the service actually **binds** it and has
no default of its own to diverge from. That holds for the two images in this repo:
their CMD is `uvicorn --port ${PORT}` and they deliberately ship no `ENV PORT`, so
the Railway variable is the single source of truth for both the bind and the URL,
and a missing one stops the container at boot rather than yielding `http://host:`.

It does not hold for the third-party image, whose URL keeps a literal port:
SearXNG hardcodes `--port 8080` in its entrypoint, so `PORT` is decoration there
and a reference to it is a guess that fails open.

Service names are case-sensitive: `${{camoufox.…}}` against a service named
`Camoufox` resolves to an empty string rather than erroring, giving `http://:8000`.

## Why Deploy Web Tools on Railway?

Railway hosts the whole five-service stack, private networking included, so you don't have to deal with configuration, and lets you scale it vertically and horizontally. Host your servers, databases, AI agents, and more in one place.

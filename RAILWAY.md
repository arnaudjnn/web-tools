# Deploy and Host Web Tools on Railway

Web Tools is an open-source web toolkit that gives AI agents eight tools to search, fetch, screenshot, crawl, and archive the web. Available as an [MCP](https://modelcontextprotocol.io/) server, REST API, and CLI. It consumes zero LLM tokens for web access, so your models spend their budget on reasoning, not searching. The web has always been free for humans, so why should AI agents have to pay per query?

## About Hosting Web Tools

This template deploys a complete self-hosted web toolkit as four services on Railway: **Redis**, **SearXNG** (privacy-respecting metasearch engine), **Crawl4AI** (headless browser for content extraction, screenshots, PDFs, and JS execution), and the **Web Tools Server** that ties them together. An API key is auto-generated at deploy time to secure your endpoint. Once deployed, any MCP-compatible client (Claude Code, Claude Desktop, Cursor, Windsurf, etc.) can connect over HTTP and use all eight tools. A REST API (`POST /api/v0/{tool_name}`) is also available for non-MCP integrations. No per-query fees, no third-party API keys, no usage limits. You own the infrastructure and the data never leaves your stack.

## Common Use Cases

- **Replace paid search APIs**: Drop-in replacement for Firecrawl, Linkup, Tavily, Exa, or Bright Data. Get web search, page fetching, and content extraction without per-query costs
- **Supercharge AI coding agents**: Connect Claude Code or Cursor to self-hosted web search and page fetching. Replace their built-in WebSearch and WebFetch tools so every search is private and free
- **Web research and monitoring**: Search the web, fetch pages as clean markdown, take screenshots, generate PDFs, execute JavaScript on pages, and query the Wayback Machine for historical snapshots
- **Build custom integrations**: Use the REST API to integrate web tools into any application or workflow

## Dependencies for Web Tools Hosting

- **Redis** (7-alpine): In-memory cache used by SearXNG for rate limiting and result caching
- **SearXNG**: Privacy-respecting metasearch engine that aggregates results from Google, Brave, DuckDuckGo, and more. Builds from `services/searxng/Dockerfile` with optional `PROXY_URL` support for outgoing requests
- **Crawl4AI**: Headless browser service for page fetching, content extraction, screenshots, PDFs, and JavaScript execution
- **Web Tools Server** (Node.js 22): The HTTP server exposing MCP and REST API endpoints. Builds from `packages/api/Dockerfile`

### Deployment Dependencies

- [Web Tools GitHub Repository](https://github.com/arnaudjnn/web-tools)
- [SearXNG Documentation](https://docs.searxng.org/)
- [Crawl4AI Documentation](https://docs.crawl4ai.com/)
- [Model Context Protocol Specification](https://modelcontextprotocol.io/)

### Implementation Details

The Web Tools Server exposes two interfaces:

**MCP** — Streamable HTTP endpoint at `/mcp` for MCP clients:

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

**REST API** — Standard HTTP endpoints at `/api/v0/{tool_name}`:

```bash
curl -X POST https://your-server.up.railway.app/api/v0/web_search \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"query": "railway deployment"}'
```

The eight tools available are: `web_search`, `web_fetch`, `web_screenshot`, `web_pdf`, `web_execute_js`, `web_crawl`, `web_snapshots`, and `web_archive`.

### Railway Service Configuration

| Service | Source | Root Directory | Notes |
| --- | --- | --- | --- |
| Web Tools Server | GitHub repo | (repo root) | Uses root `Dockerfile`, exposes MCP + REST API |
| SearXNG | GitHub repo | `services/searxng` | Optional `PROXY_URL` env var |
| Crawl4AI | Docker image (`unclecode/crawl4ai:latest`) | — | |
| Redis | Docker image (`redis:7-alpine`) | — | |

## Why Deploy Web Tools on Railway?

Railway is a singular platform to deploy your infrastructure stack. Railway will host your infrastructure so you don't have to deal with configuration, while allowing you to vertically and horizontally scale it.

By deploying Web Tools on Railway, you are one step closer to supporting a complete full-stack application with minimal burden. Host your servers, databases, AI agents, and more on Railway.

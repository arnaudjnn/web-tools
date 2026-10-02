import {
  WebSearchInput,
  WebFetchInput,
  WebHtmlInput,
  WebScreenshotInput,
  WebPdfInput,
  WebExecuteJsInput,
  WebCrawlInput,
  WebSnapshotsInput,
  WebArchiveInput,
  WebBytesInput,
  WebEvalInput,
  WebSpaFetchInput,
  WebRecycleInput,
  WebUsageStatsInput,
  WebFormSubmitInput,
  WebFormInspectInput,
  WebAgentInput,
} from './schemas.js';
import type { ToolDefinition } from './types.js';

export const tools: ToolDefinition[] = [
  {
    name: 'web_search',
    description: 'Search the web via SearXNG and return results.',
    parameters: WebSearchInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_fetch',
    description:
      'Fetch a URL and return its content as clean markdown, fetched and rendered by the ' +
      'Scrapling sidecar (residential egress + JS-challenge solving). Italian and ' +
      'bot-walled sources route through the Italian residential Firefox.',
    parameters: WebFetchInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_html',
    description:
      'Fetch a URL and return the raw HTML as served, plus the upstream status. Use this ' +
      'instead of web_fetch when you need structured markup that markdown conversion ' +
      'destroys — JSON-LD, meta tags, attributes.',
    parameters: WebHtmlInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_screenshot',
    description: 'Capture a full-page PNG screenshot of a URL and return it base64-encoded',
    parameters: WebScreenshotInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_pdf',
    description: 'Convert a URL to PDF (Chromium print-to-PDF) and return it base64-encoded',
    parameters: WebPdfInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_execute_js',
    description:
      'Execute JavaScript snippets on a URL in order and return their results as JSON',
    parameters: WebExecuteJsInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: true,
    },
  },
  {
    name: 'web_crawl',
    description:
      'Crawl one or more URLs sequentially and return one payload whose `results` array ' +
      'holds {url, status_code, success, markdown} per URL',
    parameters: WebCrawlInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_snapshots',
    description: 'List Wayback Machine snapshots for a URL',
    parameters: WebSnapshotsInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_archive',
    description: 'Retrieve an archived page from the Wayback Machine',
    parameters: WebArchiveInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_bytes',
    description:
      "Download a URL's raw bytes through a residential exit and return them base64-encoded. " +
      'Use for PDFs and other binaries behind a bot-gated or geo-sensitive origin, where ' +
      'rendering the page as text would lose the document.',
    parameters: WebBytesInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_form_inspect',
    description:
      "Read-only: list a page's visible forms as ready-to-use web_form_submit input. Per form: " +
      'action, method, fields {selector, name, type, action, label, required, placeholder, options}, ' +
      'submit_candidates, honeypot_candidates (never fill these) and a `suggested` request skeleton. ' +
      'Page-wide: captcha provider, cookie_banners (dismiss selectors) and wizard hints. Never ' +
      'fills, clicks or reveals field values; every mutating request is blocked. Radios: check one ' +
      'options[].selector. Safe to retry.',
    parameters: WebFormInspectInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_form_submit',
    description:
      'Fill fields and click submit ONCE in an isolated residential browser; returns ' +
      "form_submissions, the POST's status, ok and the resulting page. How to: call " +
      'web_form_inspect first, take its `suggested` (add values; skip honeypots), then submit ' +
      'once. Multi-step forms: gate_text, step2, step2_submit, completion_markers. ' +
      'Replay rule: replay only when the result has retryable:true (nothing was sent); ' +
      'never on outcome:"unknown" (a POST may have left) and never on an answered result. ' +
      'Persist your own reservation before calling.',
    parameters: WebFormSubmitInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: true,
    },
  },
  {
    name: 'web_agent',
    description:
      'Give a browser agent a plain-language task (e.g. "find the pricing page and list the plan ' +
      'names") and get back {final_result, success, steps[{n, action, url}], urls}. It drives a ' +
      'real Chromium, capped by max_steps and timeout_ms, and only visits allowed_domains (default: ' +
      "start_url's host). Pass output_schema for a structured final_result. The agent's browser " +
      'cannot submit forms: POSTs are blocked unless allow_mutations. With allow_form_submit it gets ' +
      'one submit through the web_form_submit path, never retried. Slower and costlier than ' +
      'web_fetch, so use it only when a page needs clicking through. "disabled" means no LLM key is ' +
      'set on the server.',
    parameters: WebAgentInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: true,
    },
  },
  {
    name: 'web_eval',
    description:
      'Evaluate JavaScript in a residential browser page and return its JSON result. Use for ' +
      'driving or inspecting a JS app (open a facet, read the codes behind it) on a site that ' +
      "bot-gates this host's own IP — web_execute_js runs from that IP and cannot reach them.",
    parameters: WebEvalInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: true,
    },
  },
  {
    name: 'web_spa_fetch',
    description:
      'Perform a same-origin in-page fetch on a warmed browser session, for origins that gate ' +
      'requests on an anti-bot sensor cookie. Stateful: one warmed page per (base_url, ' +
      'warm_path), pinned to a sticky residential exit and kept alive so the sensor stays ' +
      'validated. Returns the upstream status and body; a 403 means the sensor has not cleared.',
    parameters: WebSpaFetchInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: true,
    },
  },
  {
    name: 'web_recycle',
    description:
      'Drop the warmed session and render browser and take a fresh exit IP. Expensive (a full ' +
      'browser relaunch) and disruptive to any crawl in flight, so reach for it only when an ' +
      'exit IP has been rate-hardened by a target. For a fresh IP on one request, pass ' +
      'fresh_ip to web_eval instead.',
    parameters: WebRecycleInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: true,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: 'web_usage_stats',
    description:
      'Return process-local usage counters (per-tool call counts, approximate proxy bandwidth, estimated USD cost). In-memory only — resets on container restart; the `started_at` field lets callers detect a restart.',
    parameters: WebUsageStatsInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
];

export const toolsByName = new Map(tools.map((t) => [t.name, t]));

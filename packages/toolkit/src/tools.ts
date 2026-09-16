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
      'Fetch a URL and return its content as clean markdown. Fetched via Scrapling ' +
      '(residential egress + JS-challenge solving) and rendered to markdown by Crawl4AI.',
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
    description: 'Capture a full-page PNG screenshot of a URL via Crawl4AI',
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
    description: 'Generate a PDF document of a URL via Crawl4AI',
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
    description: 'Execute JavaScript snippets on a URL via Crawl4AI and return the crawl result',
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
    description: 'Crawl one or more URLs and extract their content using Crawl4AI',
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
    name: 'web_form_submit',
    description:
      'Fill and submit a form in a residential browser with HUMAN interaction (pointer movement, ' +
      'per-character typing, a real click on the submit control), then return the resulting page. ' +
      'Use for forms behind SCORING anti-bot (reCAPTCHA v3 and friends), which grade behaviour as ' +
      'well as exit IP — web_eval drives the same form from a page nobody touched and scores near zero.',
    parameters: WebFormSubmitInput,
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

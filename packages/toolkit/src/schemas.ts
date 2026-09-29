import { z } from 'zod';

export const WebSearchInput = z.object({
  query: z.string().min(1).describe('The search query'),
  limit: z
    .number()
    .min(1)
    .max(20)
    .optional()
    .describe('Max number of results (default: 10)'),
  engines: z
    .string()
    .optional()
    .describe(
      'Comma-separated list of engines to use (e.g. "google", "google,brave"). Overrides the default engines.',
    ),
});

export const WebFetchInput = z.object({
  url: z.string().url().describe('URL to fetch'),
  f: z
    .enum(['raw', 'fit'])
    .optional()
    .describe(
      'Content filter: fit = <body> content only (default), raw = whole document. ' +
        'Both strip scripts, styles and hidden content.',
    ),
  // No engine/mode knob on purpose. Which fetcher runs, whether it goes out
  // through the residential proxy, and whether it solves a JS challenge are
  // decided under the hood (by host, then by escalation on evidence of a
  // challenge). Callers ask for a URL; picking the way to reach it is this
  // service's job, not theirs.
  delay: z
    .number()
    .optional()
    .describe(
      'Seconds to settle after the page is stable, before returning (default: 0 — ' +
        'the fetcher already waits for stability). Raise for pages that hydrate slowly.',
    ),
});

export const WebHtmlInput = z.object({
  url: z.string().url().describe('URL to fetch'),
  // No engine/mode knob — see WebFetchInput. The parameters below are about how
  // the PAGE should be treated, not which backend runs it: a list that
  // lazy-loads into collapsed accordions is not fetchable without expanding
  // them, whichever fetcher is used. They are honoured where the backend
  // supports them and ignored where it does not.
  network_idle: z
    .boolean()
    .optional()
    .describe('Wait for the network to go quiet before returning (default: false)'),
  wait_until: z
    .enum(['load', 'domcontentloaded', 'networkidle', 'commit'])
    .optional()
    .describe('Navigation wait condition (default: load)'),
  wait_ms: z
    .number()
    .min(0)
    .max(60000)
    .optional()
    .describe('Extra settle time after load, in milliseconds'),
  click_all: z
    .array(z.string())
    .optional()
    .describe(
      'CSS selectors to click (every match) before capturing — for lists that lazy-load into ' +
        'collapsed accordions or tabs, whose content is absent otherwise.',
    ),
  settle_ms: z
    .number()
    .min(0)
    .max(30000)
    .optional()
    .describe('Time to let AJAX settle after click_all (default: 3000)'),
  fresh_ip: z
    .boolean()
    .optional()
    .describe(
      'Serve this request from a new browser context on a new exit IP, with clean cookies — ' +
        'for targets metered per IP. Costs ~1s.',
    ),
  timeout_ms: z
    .number()
    .min(1000)
    .max(180000)
    .optional()
    .describe('Upstream fetch timeout in milliseconds (default: 60000)'),
});

export const WebScreenshotInput = z.object({
  url: z.string().url().describe('URL to screenshot'),
  screenshot_wait_for: z
    .number()
    .optional()
    .describe('Seconds to wait before capture (default: 2)'),
});

export const WebPdfInput = z.object({
  url: z.string().url().describe('URL to convert to PDF'),
});

export const WebExecuteJsInput = z.object({
  url: z.string().url().describe('URL to execute scripts on'),
  scripts: z
    .array(z.string())
    .min(1)
    .describe('List of JavaScript snippets to execute in order'),
});

export const WebCrawlInput = z.object({
  urls: z
    .array(z.string().url())
    .min(1)
    .describe('URLs to crawl sequentially; each is fetched and rendered to markdown'),
  css_selector: z
    .string()
    .optional()
    .describe('Convert only elements matching this CSS selector to markdown (per page)'),
  timeout_ms: z
    .number()
    .min(1000)
    .max(180000)
    .optional()
    .describe('Per-URL fetch timeout in milliseconds (default: 60000)'),
});

export const WebSnapshotsInput = z.object({
  url: z.string().describe('URL to check for snapshots'),
  from: z.string().optional().describe('Start date in YYYYMMDD format'),
  to: z.string().optional().describe('End date in YYYYMMDD format'),
  limit: z
    .number()
    .optional()
    .describe('Max number of snapshots to return (default: 100)'),
  match_type: z
    .enum(['exact', 'prefix', 'host', 'domain'])
    .optional()
    .describe('URL matching strategy (default: exact)'),
  filter: z
    .array(z.string())
    .optional()
    .describe('CDX API filters (e.g. ["statuscode:200", "mimetype:text/html"])'),
});

export const WebArchiveInput = z.object({
  url: z.string().describe('URL of the page to retrieve'),
  timestamp: z.string().describe('Timestamp in YYYYMMDDHHMMSS format'),
  original: z
    .boolean()
    .optional()
    .describe('Get original content without Wayback Machine banner (default: false)'),
});

export const WebBytesInput = z.object({
  url: z.string().url().describe('URL of the binary to download (e.g. a PDF)'),
  timeout_ms: z.number().min(1000).max(180000).optional().describe('Fetch timeout (default: 60000)'),
});

export const WebFormSubmitInput = z.object({
  url: z.string().url().describe('URL of the page holding the form'),
  fields: z
    .array(
      z.object({
        selector: z.string().describe('CSS selector of the control'),
        value: z.string().optional().describe('text to type, or option value for action=select'),
        action: z.enum(['type', 'check', 'select']).optional().describe('default: type'),
      }),
    )
    .describe('Controls to fill, in order'),
  submit: z.string().describe('CSS selector of the submit control'),
  dismiss: z.array(z.string()).optional().describe('Selectors clicked first (cookie walls)'),
  success_url: z.string().optional().describe('Regex; a final URL matching it means success'),
  submission_urls: z.array(z.string().url()).min(1).max(10).optional().describe('Same-origin form POST URLs sharing one submission budget; defaults to url'),
  captcha_field: z.string().min(1).max(100).optional().describe('POST field to check for token presence, never its value'),
  require_captcha_token: z.boolean().optional().describe('Block the form POST when its CAPTCHA field is empty/unreadable; no retry'),
  ready_expression: z.string().min(1).max(2000).optional().describe('Main-world boolean expression required before clicking submit'),
  inspect_only: z.boolean().optional().describe('Navigate without filling/clicking; block same-origin mutating requests'),
  wait_until: z.enum(['load', 'domcontentloaded', 'networkidle', 'commit']).optional(),
  wait_ms: z.number().min(0).max(60000).optional().describe('Settle after load (default: 4000)'),
  settle_ms: z.number().min(1000).max(120000).optional().describe('Wait for the outcome (default: 20000)'),
  timeout_ms: z.number().min(1000).max(180000).optional(),
  fresh_ip: z.boolean().optional().describe('New context + exit IP (default: true)'),
  exit_session: z
    .string()
    .optional()
    .describe('Pin the exit: same token = same IP, so a passing exit can be reused instead of re-searched'),
  headed: z
    .boolean()
    .optional()
    .describe('Headed browser under xvfb for score-gated forms; headless fleets score 0 on reCAPTCHA v3'),
});

export const WebEvalInput = z.object({
  url: z.string().url().describe('URL to open'),
  js: z
    .string()
    .describe('JS expression or IIFE evaluated in the page; must return JSON-serialisable data'),
  wait_until: z
    .enum(['load', 'domcontentloaded', 'networkidle', 'commit'])
    .optional()
    .describe('Navigation wait condition (default: networkidle)'),
  wait_ms: z.number().min(0).max(60000).optional().describe('Extra settle time after load (default: 6000)'),
  timeout_ms: z.number().min(1000).max(180000).optional().describe('Navigation timeout (default: 90000)'),
  fresh_ip: z
    .boolean()
    .optional()
    .describe(
      'Serve this one request from a new browser context on a new exit IP, with clean cookies — ' +
        'for targets metered per IP. Costs ~1s, unlike web_recycle.',
    ),
});

export const WebSpaFetchInput = z.object({
  base_url: z.string().url().describe('Origin to warm and fetch against'),
  warm_path: z.string().optional().describe('Path navigated to warm the sensor (default: /)'),
  method: z.string().optional().describe('HTTP method for the in-page fetch (default: GET)'),
  path: z.string().describe('Same-origin path for the in-page fetch'),
  body: z.record(z.unknown()).nullable().optional().describe('JSON body, sent as a JSON string'),
  accept: z.string().optional().describe('Accept header (default: application/json)'),
  sensor_wait_ms: z
    .number()
    .min(0)
    .max(120000)
    .optional()
    .describe('Time spent seeding the sensor on a (re)warm (default: 20000)'),
  mature_probe: z
    .record(z.unknown())
    .nullable()
    .optional()
    .describe(
      'Optional {method,path,body,accept} probe used during warmup: interaction loops until this ' +
        'stops returning 403, i.e. until the sensor cookie is accepted.',
    ),
  mature_max_tries: z.number().min(1).max(20).optional().describe('Maturation attempts (default: 6)'),
  timeout_ms: z.number().min(1000).max(300000).optional().describe('Client timeout (default: 180000)'),
});

export const WebRecycleInput = z
  .object({})
  .describe('Drop the warmed session and render browser, and take a fresh exit IP. No parameters.');

export const WebUsageStatsInput = z
  .object({})
  .describe('Process-local usage counters. No parameters.');

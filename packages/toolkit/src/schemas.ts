import { z } from 'zod';

/**
 * The longest browser run Scrapling allows (MAX_FETCH_MS in
 * services/scrapling/app.py; a test keeps the two equal). Fetch-shaped tools
 * reject anything above it instead of letting the sidecar cap it silently.
 */
export const MAX_FETCH_TIMEOUT_MS = 90_000;
/** web_crawl: URLs per call, and the whole call's wall-clock budget. */
export const MAX_CRAWL_URLS = 20;
export const CRAWL_DEADLINE_MS = 300_000;

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
    .min(0)
    .max(60)
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
    .max(MAX_FETCH_TIMEOUT_MS)
    .optional()
    .describe(`Upstream fetch timeout in milliseconds (default: 60000, max: ${MAX_FETCH_TIMEOUT_MS})`),
});

export const WebScreenshotInput = z.object({
  url: z.string().url().describe('URL to screenshot'),
  screenshot_wait_for: z
    .number()
    .min(0)
    .max(60)
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
    .max(MAX_CRAWL_URLS)
    .describe(
      `URLs to crawl sequentially (max ${MAX_CRAWL_URLS}); each is fetched and rendered to markdown. ` +
        `The whole crawl stops after ${CRAWL_DEADLINE_MS / 1000}s; URLs not reached by then come back ` +
        'with success:false.',
    ),
  css_selector: z
    .string()
    .optional()
    .describe('Convert only elements matching this CSS selector to markdown (per page)'),
  timeout_ms: z
    .number()
    .min(1000)
    .max(MAX_FETCH_TIMEOUT_MS)
    .optional()
    .describe(`Per-URL fetch timeout in milliseconds (default: 60000, max: ${MAX_FETCH_TIMEOUT_MS})`),
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

// One control to fill; web_form_inspect's fields[].selector/action map 1:1.
const FormField = z.object({
  selector: z.string().describe('CSS selector of the control (from web_form_inspect)'),
  value: z.string().optional().describe('Text to type, or the option value for action=select'),
  action: z.enum(['type', 'check', 'select']).optional().describe('Default: type'),
});

const ProfileName = z
  .string()
  .regex(/^[A-Za-z0-9][A-Za-z0-9._-]*$/)
  .max(64);

export const WebFormResultInput = z.object({
  job_id: z.string().min(1).describe('The job_id an async web_form_submit returned'),
});

export const WebFormSubmitInput = z.object({
  url: z.string().url().describe('URL of the page holding the form'),
  async: z
    .boolean()
    .optional()
    .describe('Return {job_id} at once and run the submission in the background; poll web_form_result. Use it to queue many forms'),
  fields: z.array(FormField).describe('Controls to fill, in order. Never include honeypot_candidates'),
  submit: z.string().describe('CSS selector of the submit control, clicked exactly once'),
  dismiss: z.array(z.string()).optional().describe('Cookie-banner selectors clicked before filling'),
  success_url: z.string().optional().describe('Regex; a final URL matching it means success'),
  submission_urls: z
    .array(z.string().url())
    .min(1)
    .max(10)
    .optional()
    .describe('Same-origin POST URLs counted as the submission (default: url)'),
  captcha_field: z.string().min(1).max(100).optional().describe('POST field checked for token presence (never its value)'),
  require_captcha_token: z.boolean().optional().describe('Block the POST when captcha_field is empty; no retry'),
  ready_expression: z.string().min(1).max(2000).optional().describe('Main-world boolean expression required before clicking submit'),
  inspect_only: z.boolean().optional().describe('Navigate only (no fill/click); prefer web_form_inspect'),
  wait_until: z.enum(['load', 'domcontentloaded', 'networkidle', 'commit']).optional(),
  wait_ms: z.number().min(0).max(60000).optional().describe('Settle after load (default: 4000)'),
  settle_ms: z.number().min(1000).max(120000).optional().describe('Wait for the outcome (default: 20000)'),
  timeout_ms: z
    .number()
    .min(1000)
    .max(540000)
    .optional()
    .describe(
      'Whole-run deadline incl. any retries (default: 300000, room for a few ~55 s attempts; a gated wizard needs more). Above 360000 only with retry_on_captcha_rejection',
    ),
  fresh_ip: z.boolean().optional().describe('New context + exit IP (default: true)'),
  exit_session: z
    .string()
    .optional()
    .describe('Pin the exit IP: the same token reuses the same IP (keep one that passed)'),
  profile: ProfileName.optional().describe(
    'Named persistent browser profile: cookies + fingerprint reused across calls (default: isolated)',
  ),
  headed: z.boolean().optional().describe('Headed browser under Xvfb (default: true; reCAPTCHA v3 scores headless at 0)'),
  gate_text: z
    .string()
    .min(1)
    .max(300)
    .optional()
    .describe('Wizard: regex on a button/link shown after the first POST; clicked once'),
  step2: z
    .array(FormField)
    .optional()
    .describe("Wizard: the second step's controls, filled only if that step renders"),
  step2_submit: z
    .string()
    .min(1)
    .max(300)
    .optional()
    .describe("Wizard: step2's submit selector (default: 'form button')"),
  completion_markers: z
    .array(z.string().min(1))
    .max(10)
    .optional()
    .describe('Wizard: regexes on body text meaning done when the URL never changes'),
  sticky_exit: z
    .boolean()
    .optional()
    .describe("With a profile: reuse the exit pinned in the profile (pin one on first use). Default: the sidecar's FORM_PROFILE_STICKY_EXIT"),
  stop_after_posts: z
    .number()
    .int()
    .min(1)
    .max(3)
    .optional()
    .describe('Wizard warm-up: return after this many POSTs (1 = first step only)'),
  score_gate: z
    .boolean()
    .optional()
    .describe(
      'Probe candidate exits on our reCAPTCHA oracle first (same launch config) and run the form on the first that scores >= score_threshold. Default: off (retries on a fresh exit are faster at the same success); true enables. Nothing reaches the target before it passes; no passing exit = 503 retryable no_scoring_exit',
    ),
  score_threshold: z.number().min(0).max(1).optional().describe('Score gate threshold (default: 0.7)'),
  score_gate_tries: z.number().int().min(1).max(6).optional().describe('Exits the score gate probes at most (default: 3)'),
  retry_on_captcha_rejection: z
    .number()
    .int()
    .min(0)
    .max(4)
    .optional()
    .describe(
      'Fresh attempts (new context, new exit) after an explicit step-0 CAPTCHA refusal (one 2xx POST, same URL, every error node matching captcha_rejection_text) or any provably zero-POST failure. Never with a pinned exit_session/profile; each must fit timeout_ms. Default 3; 0 = single attempt. Result adds attempts[] and form_submissions is the total',
    ),
  pacing: z
    .enum(['auto', 'fast', 'lab', 'lab_fast', 'default'])
    .optional()
    .describe("Input cadence. auto (default): human typing rhythm when the page loads a CAPTCHA (reCAPTCHA, hCaptcha, Turnstile...), fast when it loads none. fast / lab force one"),
  captcha_rejection_text: z
    .string()
    .min(1)
    .max(300)
    .optional()
    .describe("Regex (case-insensitive) every error node of the re-rendered form must match (default: 'error verifying recaptcha|captcha (?:non |in)?valid|recaptcha')"),
  captcha_lib_direct: z
    .boolean()
    .optional()
    .describe(
      "Fetch reCAPTCHA's static library files (www.gstatic.com/recaptcha/releases/...) direct instead of through the exit, falling back to the exit on failure; identity-bearing google.com requests always use the exit. Default: the sidecar's FORM_CAPTCHA_LIB_DIRECT (off)",
    ),
});

export const WebFormInspectInput = z.object({
  url: z.string().url().describe('URL of the page holding the form'),
  wait_until: z.enum(['load', 'domcontentloaded', 'networkidle', 'commit']).optional(),
  wait_ms: z.number().min(0).max(60000).optional().describe('Settle after load so late forms/banners render (default: 4000)'),
  timeout_ms: z.number().min(1000).max(180000).optional().describe('Whole-run deadline (default: 60000)'),
  fresh_ip: z.boolean().optional().describe('New context + exit IP (default: true)'),
  exit_session: z.string().optional().describe('Pin the exit IP; reuse the token in web_form_submit to submit from the same IP'),
  profile: ProfileName.optional().describe('Named persistent profile; the visit warms it for a later submit'),
  headed: z.boolean().optional().describe('Headed browser (as web_form_submit)'),
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

export const WebAgentInput = z.object({
  task: z.string().min(1).max(4000).describe('What to do, in plain language'),
  start_url: z.string().url().optional().describe('Page opened before the first step'),
  max_steps: z.number().int().min(1).max(40).optional().describe('Step cap (default: 15)'),
  allowed_domains: z
    .array(z.string().min(1))
    .max(20)
    .optional()
    .describe("Hosts the agent may visit, subdomains included (default: start_url's host)"),
  output_schema: z
    .record(z.unknown())
    .optional()
    .describe('JSON schema (type=object, with properties) the final_result must match'),
  timeout_ms: z
    .number()
    .int()
    .min(10000)
    .max(300000)
    .optional()
    .describe('Whole-run deadline; a partial result comes back at the deadline (default: 180000)'),
  stealth: z.boolean().optional().describe('Residential egress instead of direct (default: false)'),
  allow_mutations: z
    .boolean()
    .optional()
    .describe("Let the agent's own browser send POST/PUT/DELETE (default: false — blocked)"),
  allow_form_submit: z
    .boolean()
    .optional()
    .describe('Give the agent ONE submit through the web_form_submit path (default: false)'),
});

export const WebRecycleInput = z
  .object({})
  .describe('Drop the warmed session and render browser, and take a fresh exit IP. No parameters.');

export const WebUsageStatsInput = z
  .object({})
  .describe('Process-local usage counters. No parameters.');

// REST-only stealth-score diagnostics: the sidecar's pydantic models validate
// the fields (services/camoufox/score_probe.py), so this stays an open record.
export const WebStealthDiagnosticInput = z
  .object({})
  .passthrough()
  .describe('Passed through to the Camoufox stealth-score endpoint (fields: score_probe.py).');

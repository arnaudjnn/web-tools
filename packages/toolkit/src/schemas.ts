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
    .enum(['raw', 'fit', 'bm25', 'llm'])
    .optional()
    .describe('Content-filter strategy (default: fit)'),
  q: z.string().optional().describe('Query string for BM25/LLM filters'),
  c: z.boolean().optional().describe('Enable caching for the request'),
  provider: z.string().optional().describe('LLM provider for LLM filter (e.g. "openai/gpt-4")'),
  temperature: z.number().optional().describe('Temperature for LLM filter'),
  base_url: z.string().optional().describe('Base URL override for the LLM provider'),
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
  urls: z.array(z.string().url()).min(1).describe('List of URLs to crawl'),
  browser_config: z
    .record(z.unknown())
    .optional()
    .describe('Optional Crawl4AI browser configuration'),
  crawler_config: z
    .object({
      // Content Processing
      word_count_threshold: z.number().optional().describe('Minimum word count threshold for content blocks (default: ~200)'),
      css_selector: z.string().optional().describe('CSS selector to target specific page elements for extraction'),
      target_elements: z.array(z.string()).optional().describe('List of CSS selectors for target elements'),
      excluded_tags: z.array(z.string()).optional().describe('HTML tags to exclude from extraction'),
      excluded_selector: z.string().optional().describe('CSS selector for elements to exclude'),
      only_text: z.boolean().optional().describe('Strip all HTML and return plain text only'),
      prettiify: z.boolean().optional().describe('Prettify the HTML output'),
      keep_data_attributes: z.boolean().optional().describe('Preserve data-* attributes in output'),
      keep_attrs: z.array(z.string()).optional().describe('List of HTML attributes to preserve'),
      remove_forms: z.boolean().optional().describe('Remove form elements from output'),
      parser_type: z.string().optional().describe('HTML parser type (default: "lxml")'),

      // Page Navigation & Timing
      wait_until: z.string().optional().describe('Page load event to wait for (default: "domcontentloaded")'),
      page_timeout: z.number().optional().describe('Page load timeout in milliseconds (default: 60000)'),
      wait_for: z.string().optional().describe('CSS selector to wait for before extracting content'),
      wait_for_timeout: z.number().optional().describe('Timeout in ms for wait_for selector'),
      wait_for_images: z.boolean().optional().describe('Wait for images to load before extraction'),
      delay_before_return_html: z.number().optional().describe('Delay in seconds before extracting HTML (default: 0.1)'),
      mean_delay: z.number().optional().describe('Mean delay between actions in seconds (default: 0.1)'),
      max_range: z.number().optional().describe('Max random range added to delays (default: 0.3)'),
      semaphore_count: z.number().optional().describe('Max concurrent operations (default: 5)'),

      // Page Interaction
      js_code: z.union([z.string(), z.array(z.string())]).optional().describe('JavaScript code to execute on the page before extraction'),
      js_only: z.boolean().optional().describe('Only execute JS without re-fetching the page (requires session_id)'),
      ignore_body_visibility: z.boolean().optional().describe('Proceed even if body is not visible (default: true)'),
      scan_full_page: z.boolean().optional().describe('Scroll through the entire page to trigger lazy-loaded content'),
      scroll_delay: z.number().optional().describe('Delay between scroll steps in seconds (default: 0.2)'),
      max_scroll_steps: z.number().optional().describe('Maximum number of scroll steps'),
      process_iframes: z.boolean().optional().describe('Extract content from iframes'),
      flatten_shadow_dom: z.boolean().optional().describe('Flatten shadow DOM elements for extraction'),
      remove_overlay_elements: z.boolean().optional().describe('Remove popup/overlay elements blocking content'),
      remove_consent_popups: z.boolean().optional().describe('Automatically dismiss cookie consent and privacy popups'),
      simulate_user: z.boolean().optional().describe('Simulate real user behavior to bypass bot detection'),
      override_navigator: z.boolean().optional().describe('Override navigator properties to avoid bot detection'),
      magic: z.boolean().optional().describe('Enable all anti-bot measures at once'),
      adjust_viewport_to_content: z.boolean().optional().describe('Adjust viewport size to fit page content'),

      // Caching & Session
      cache_mode: z.string().optional().describe('Cache mode for the crawl'),
      session_id: z.string().optional().describe('Session ID to reuse browser session across crawls'),

      // Media Handling
      screenshot: z.boolean().optional().describe('Capture a screenshot of the page'),
      screenshot_wait_for: z.number().optional().describe('Delay in seconds before taking screenshot'),
      pdf: z.boolean().optional().describe('Capture page as PDF'),
      exclude_external_images: z.boolean().optional().describe('Exclude external images from output'),
      exclude_all_images: z.boolean().optional().describe('Exclude all images from output'),

      // Link Handling
      exclude_external_links: z.boolean().optional().describe('Remove external links from output'),
      exclude_social_media_links: z.boolean().optional().describe('Remove social media links from output'),
      exclude_social_media_domains: z.array(z.string()).optional().describe('List of social media domains to exclude'),
      exclude_domains: z.array(z.string()).optional().describe('List of domains to exclude links from'),
      exclude_internal_links: z.boolean().optional().describe('Remove internal links from output'),

      // HTTP & Identity
      method: z.string().optional().describe('HTTP method for the request (default: "GET")'),
      user_agent: z.string().optional().describe('Custom user agent string'),
      user_agent_mode: z.string().optional().describe('User agent generation mode'),

      // Debug
      verbose: z.boolean().optional().describe('Enable verbose logging (default: true)'),
      log_console: z.boolean().optional().describe('Log browser console messages'),

      // Robots & Compliance
      check_robots_txt: z.boolean().optional().describe('Check and respect robots.txt rules'),
    })
    .passthrough()
    .optional()
    .describe('Optional Crawl4AI crawler configuration'),
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

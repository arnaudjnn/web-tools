import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express, { Request, Response } from 'express';
import { z } from 'zod';

import { Config } from './config.js';
import {
  callCrawlTool,
  callExecuteJsTool,
  callMdTool,
  callPdfTool,
  callScreenshotTool,
} from './crawl4ai.js';
import { searchSearXNG } from './searxng.js';
import { getArchivedPage, getSnapshots } from './wayback.js';

// Type for MCP tool call results
type ToolResult = {
  content: { type: 'text'; text: string }[];
  isError?: boolean;
};

// Wrap a Crawl4AI proxy call with proper error surfacing
async function proxyCrawl4AI(
  toolName: string,
  fn: () => Promise<unknown>,
): Promise<ToolResult> {
  try {
    const resolved = (await fn()) as ToolResult;

    if (resolved?.isError) {
      const text =
        resolved.content?.[0]?.text ||
        JSON.stringify(resolved.content) ||
        '(no details returned)';
      log(`Crawl4AI ${toolName} error response:`, text);
      return {
        content: [{ type: 'text' as const, text: `Crawl4AI ${toolName} error: ${text}` }],
        isError: true,
      };
    }

    if (
      !resolved?.content ||
      resolved.content.length === 0 ||
      resolved.content.every(c => !c.text)
    ) {
      log(`Crawl4AI ${toolName} returned empty content`);
      return {
        content: [
          {
            type: 'text' as const,
            text: `Crawl4AI ${toolName} returned empty content. The page may have no extractable text or the crawl may have timed out.`,
          },
        ],
        isError: true,
      };
    }

    return resolved;
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    log(`Crawl4AI ${toolName} threw:`, msg);
    return {
      content: [{ type: 'text' as const, text: `Crawl4AI ${toolName} error: ${msg}` }],
      isError: true,
    };
  }
}

// Helper function to log to stderr
const log = (...args: any[]) => {
  process.stderr.write(
    args
      .map(arg => (typeof arg === 'string' ? arg : JSON.stringify(arg)))
      .join(' ') + '\n',
  );
};

// Function to create and configure a new server instance for each request
function createServer(): McpServer {
  const server = new McpServer(
    {
      name: 'web_tools',
      version: '1.0.0',
    },
    { capabilities: { logging: {} } },
  );

  // Web search tool — lightweight, no LLM needed
  server.tool(
    'web_search',
    'Search the web via SearXNG and return results.',
    {
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
    },
    async ({ query, limit, engines }) => {
      try {
        const results = await searchSearXNG(query, { limit: limit ?? 10, engines });
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(results.data, null, 2),
            },
          ],
        };
      } catch (error) {
        return {
          content: [
            {
              type: 'text',
              text: `Search error: ${error instanceof Error ? error.message : String(error)}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // web_fetch tool — proxy to Crawl4AI md tool
  server.tool(
    'web_fetch',
    'Fetch a URL and return its content as clean markdown via Crawl4AI',
    {
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
    },
    async args => proxyCrawl4AI('md', () => callMdTool(args)),
  );

  // web_screenshot tool — proxy to Crawl4AI screenshot tool
  server.tool(
    'web_screenshot',
    'Capture a full-page PNG screenshot of a URL via Crawl4AI',
    {
      url: z.string().url().describe('URL to screenshot'),
      screenshot_wait_for: z
        .number()
        .optional()
        .describe('Seconds to wait before capture (default: 2)'),
    },
    async args => proxyCrawl4AI('screenshot', () => callScreenshotTool(args)),
  );

  // web_pdf tool — proxy to Crawl4AI pdf tool
  server.tool(
    'web_pdf',
    'Generate a PDF document of a URL via Crawl4AI',
    {
      url: z.string().url().describe('URL to convert to PDF'),
    },
    async args => proxyCrawl4AI('pdf', () => callPdfTool(args)),
  );

  // web_execute_js tool — proxy to Crawl4AI execute_js tool
  server.tool(
    'web_execute_js',
    'Execute JavaScript snippets on a URL via Crawl4AI and return the crawl result',
    {
      url: z.string().url().describe('URL to execute scripts on'),
      scripts: z
        .array(z.string())
        .min(1)
        .describe('List of JavaScript snippets to execute in order'),
    },
    async args => proxyCrawl4AI('execute_js', () => callExecuteJsTool(args)),
  );

  // web_crawl tool — proxy to Crawl4AI MCP server
  server.tool(
    'web_crawl',
    'Crawl one or more URLs and extract their content using Crawl4AI',
    {
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
          scan_full_page: z.boolean().optional().describe('Scroll through the entire page to trigger lazy-loaded content. May fail on heavy infinite-scroll pages; increase page_timeout if needed'),
          scroll_delay: z.number().optional().describe('Delay between scroll steps in seconds (default: 0.2)'),
          max_scroll_steps: z.number().optional().describe('Maximum number of scroll steps'),
          process_iframes: z.boolean().optional().describe('Extract content from iframes'),
          flatten_shadow_dom: z.boolean().optional().describe('Flatten shadow DOM elements for extraction'),
          remove_overlay_elements: z.boolean().optional().describe('Remove popup/overlay elements blocking content'),
          remove_consent_popups: z.boolean().optional().describe('Automatically dismiss cookie consent and privacy popups'),
          simulate_user: z.boolean().optional().describe('Simulate real user behavior (random scrolls, mouse movements, delays) to bypass bot detection'),
          override_navigator: z.boolean().optional().describe('Override navigator properties to avoid bot detection'),
          magic: z.boolean().optional().describe('Enable all anti-bot measures at once (simulate_user, override_navigator, etc.). Preferred over setting individual anti-bot params'),
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
    },
    async args => proxyCrawl4AI('crawl', () => callCrawlTool(args)),
  );

  // web_snapshots tool — Wayback Machine CDX API
  server.tool(
    'web_snapshots',
    'List Wayback Machine snapshots for a URL',
    {
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
        .describe(
          'CDX API filters (e.g. ["statuscode:200", "mimetype:text/html"])',
        ),
    },
    async ({ url, from, to, limit, match_type, filter }) => {
      try {
        const snapshots = await getSnapshots({
          url,
          from,
          to,
          limit,
          matchType: match_type,
          filter,
        });
        if (snapshots.length === 0) {
          return {
            content: [
              { type: 'text', text: `No snapshots found for URL: ${url}` },
            ],
          };
        }
        return {
          content: [{ type: 'text', text: JSON.stringify(snapshots, null, 2) }],
        };
      } catch (error) {
        return {
          content: [
            {
              type: 'text' as const,
              text: `Snapshots error: ${error instanceof Error ? error.message : String(error)}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  // web_archive tool — Wayback Machine page retrieval
  server.tool(
    'web_archive',
    'Retrieve an archived page from the Wayback Machine',
    {
      url: z.string().describe('URL of the page to retrieve'),
      timestamp: z.string().describe('Timestamp in YYYYMMDDHHMMSS format'),
      original: z
        .boolean()
        .optional()
        .describe(
          'Get original content without Wayback Machine banner (default: false)',
        ),
    },
    async ({ url, timestamp, original }) => {
      try {
        const { waybackUrl, content } = await getArchivedPage({
          url,
          timestamp,
          original,
        });
        const MAX_LENGTH = 50000;
        const truncated = content.length > MAX_LENGTH;
        const text = `Wayback URL: ${waybackUrl}\nContent length: ${content.length} characters\n\n${truncated ? content.substring(0, MAX_LENGTH) + '\n\n[Content truncated]' : content}`;
        return { content: [{ type: 'text', text }] };
      } catch (error) {
        return {
          content: [
            {
              type: 'text' as const,
              text: `Archive error: ${error instanceof Error ? error.message : String(error)}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  return server;
}

// Log environment check
log('Environment check:', {
  searxngUrl: Config.searxng.url,
});

const app = express();
app.use(express.json());

// API key auth middleware — skips /health
app.use((req: Request, res: Response, next) => {
  if (req.path === '/health') return next();

  const provided =
    req.headers.authorization?.replace(/^Bearer\s+/i, '') ||
    (req.query.api_key as string);

  if (provided !== Config.apiKey) {
    res.status(403).json({
      error: 'forbidden',
      error_description: 'Invalid or missing API key',
    });
    return;
  }

  next();
});

app.post('/mcp', async (req: Request, res: Response) => {
  const server = createServer();
  try {
    const transport: StreamableHTTPServerTransport =
      new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
      });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);

    res.on('close', () => {
      log('Request closed');
      transport.close();
      server.close();
    });
  } catch (error) {
    log('Error handling MCP request:', error);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: '2.0',
        error: {
          code: -32603,
          message: 'Internal server error',
        },
        id: null,
      });
    }
  }
});

app.get('/health', (_req: Request, res: Response) => {
  res.json({ status: 'ok' });
});

app.get('/mcp', async (_req: Request, res: Response) => {
  res.writeHead(405).end(
    JSON.stringify({
      jsonrpc: '2.0',
      error: {
        code: -32000,
        message: 'Method not allowed.',
      },
      id: null,
    }),
  );
});

app.delete('/mcp', async (req: Request, res: Response) => {
  log('Received DELETE MCP request');
  res.writeHead(405).end(
    JSON.stringify({
      jsonrpc: '2.0',
      error: {
        code: -32000,
        message: 'Method not allowed.',
      },
      id: null,
    }),
  );
});

// Start the server
const PORT = parseInt(process.env.PORT || '3000', 10);
app.listen(PORT, () => {
  log(`MCP Stateless Streamable HTTP Server listening on port ${PORT}`);
});

// Handle server shutdown
process.on('SIGINT', async () => {
  log('Shutting down server...');
  process.exit(0);
});

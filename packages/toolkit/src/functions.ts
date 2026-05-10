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
import type { ToolResult } from './types.js';

const log = (...args: unknown[]) => {
  process.stderr.write(
    args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n',
  );
};

// ── Crawl4AI proxy wrapper ───────────────────────────────────────────

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
        content: [{ type: 'text', text: `Crawl4AI ${toolName} error: ${text}` }],
        isError: true,
      };
    }

    if (
      !resolved?.content ||
      resolved.content.length === 0 ||
      resolved.content.every((c) => !c.text)
    ) {
      log(`Crawl4AI ${toolName} returned empty content`);
      return {
        content: [
          {
            type: 'text',
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
      content: [{ type: 'text', text: `Crawl4AI ${toolName} error: ${msg}` }],
      isError: true,
    };
  }
}

// ── Tool handler functions ───────────────────────────────────────────

export async function web_search(params: {
  query: string;
  limit?: number;
  engines?: string;
}) {
  const results = await searchSearXNG(params.query, {
    limit: params.limit ?? 10,
    engines: params.engines,
  });
  return results.data;
}

export async function web_fetch(params: Record<string, unknown>): Promise<ToolResult> {
  // The upstream Crawl4AI `md` MCP tool is unstable on this version
  // (BrowserContext.new_page: Connection closed while reading from the driver).
  // Route through the working `crawl` tool and extract markdown ourselves.
  const url = params.url as string | undefined;
  if (!url) {
    return {
      content: [{ type: 'text', text: 'web_fetch error: missing required `url`' }],
      isError: true,
    };
  }
  const filter = ((params.f as string | undefined) ?? 'fit').toLowerCase();

  const browserParams: Record<string, unknown> = { headless: true };
  if (Config.proxy) {
    browserParams.proxy_config = {
      type: 'ProxyConfig',
      params: {
        server: Config.proxy.server,
        username: Config.proxy.username,
        password: Config.proxy.password,
      },
    };
  }

  return proxyCrawl4AI('crawl', async () => {
    const resp = (await callCrawlTool({
      urls: [url],
      browser_config: { type: 'BrowserConfig', params: browserParams },
      crawler_config: {
        type: 'CrawlerRunConfig',
        params: {
          magic: true,
          simulate_user: true,
          override_navigator: true,
          remove_overlay_elements: true,
          remove_consent_popups: true,
          wait_until: 'domcontentloaded',
          page_timeout: 90000,
          delay_before_return_html: 4,
        },
      },
    })) as ToolResult;

    const text = resp?.content?.[0]?.text;
    if (!text) return resp;

    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return resp;
    }
    const r = (parsed as { results?: Array<Record<string, unknown>> })?.results?.[0];
    if (!r) return resp;

    let md = '';
    const m = r.markdown as string | { raw_markdown?: string; fit_markdown?: string } | undefined;
    if (typeof m === 'string') {
      md = m;
    } else if (m && typeof m === 'object') {
      md =
        (filter === 'raw' ? m.raw_markdown : m.fit_markdown) || m.raw_markdown || m.fit_markdown || '';
    }
    if (!md) return resp;

    return {
      content: [{ type: 'text', text: md }],
      isError: !r.success,
    };
  });
}

export async function web_screenshot(params: Record<string, unknown>): Promise<ToolResult> {
  return proxyCrawl4AI('screenshot', () => callScreenshotTool(params));
}

export async function web_pdf(params: Record<string, unknown>): Promise<ToolResult> {
  return proxyCrawl4AI('pdf', () => callPdfTool(params));
}

export async function web_execute_js(params: Record<string, unknown>): Promise<ToolResult> {
  return proxyCrawl4AI('execute_js', () => callExecuteJsTool(params));
}

export async function web_crawl(params: Record<string, unknown>): Promise<ToolResult> {
  return proxyCrawl4AI('crawl', () => callCrawlTool(params));
}

export async function web_snapshots(params: {
  url: string;
  from?: string;
  to?: string;
  limit?: number;
  match_type?: 'exact' | 'prefix' | 'host' | 'domain';
  filter?: string[];
}) {
  const snapshots = await getSnapshots({
    url: params.url,
    from: params.from,
    to: params.to,
    limit: params.limit,
    matchType: params.match_type,
    filter: params.filter,
  });
  return snapshots;
}

export async function web_archive(params: {
  url: string;
  timestamp: string;
  original?: boolean;
}) {
  const { waybackUrl, content } = await getArchivedPage(params);
  const MAX_LENGTH = 50000;
  const truncated = content.length > MAX_LENGTH;
  return {
    waybackUrl,
    contentLength: content.length,
    content: truncated
      ? content.substring(0, MAX_LENGTH) + '\n\n[Content truncated]'
      : content,
  };
}

// ── Function map ─────────────────────────────────────────────────────

export const functionMap: Record<string, (params: any) => Promise<any>> = {
  web_search,
  web_fetch,
  web_screenshot,
  web_pdf,
  web_execute_js,
  web_crawl,
  web_snapshots,
  web_archive,
};

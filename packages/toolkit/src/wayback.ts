import { camoufoxBytes } from './camoufox.js';
import { errMsg, log } from './log.js';
import { renderMarkdown } from './markdown.js';
import { scraplingRaw } from './scrapling.js';
import type { SnapshotInfo } from './types.js';

const CDX_API_URL = 'https://web.archive.org/cdx/search/cdx';
const WAYBACK_BASE_URL = 'https://web.archive.org/web';

/**
 * GET a web.archive.org URL from a residential exit, never from this process:
 * archive.org silently drops this project's datacenter egress (the connection
 * hangs). Scrapling's /raw picks its residential exit by host (STEALTH_HOSTS);
 * when Scrapling is down, Camoufox's /bytes leaves on the Italian residential
 * exit instead. Redirects are followed either way (wayback 302s to the
 * canonical timestamp); only /raw reports the final URL.
 */
async function archiveGet(url: string, timeoutMs: number): Promise<{ status: number; url: string; body: string }> {
  try {
    const r = await scraplingRaw({ url, timeoutMs });
    return { status: r.status, url: r.url, body: r.body };
  } catch (err) {
    log('wayback: scrapling /raw failed, trying camoufox /bytes:', errMsg(err));
    try {
      const r = await camoufoxBytes({ url, timeoutMs });
      return { status: r.status, url, body: Buffer.from(r.b64, 'base64').toString('utf8') };
    } catch (err2) {
      throw new Error(`scrapling: ${errMsg(err)}; camoufox: ${errMsg(err2)}`);
    }
  }
}

function formatTimestamp(ts: string): string {
  if (ts.length !== 14) return ts;
  return `${ts.substring(0, 4)}-${ts.substring(4, 6)}-${ts.substring(6, 8)} ${ts.substring(8, 10)}:${ts.substring(10, 12)}:${ts.substring(12, 14)}`;
}

export async function getSnapshots(params: {
  url: string;
  from?: string;
  to?: string;
  limit?: number;
  matchType?: 'exact' | 'prefix' | 'host' | 'domain';
  filter?: string[];
}): Promise<SnapshotInfo[]> {
  const { url, from, to, limit = 100, matchType = 'exact', filter } = params;

  const qs = new URLSearchParams({
    url,
    output: 'json',
    fl: 'timestamp,original,mimetype,statuscode,digest,length',
    collapse: 'timestamp:8',
    limit: String(limit),
  });
  if (from) qs.set('from', from);
  if (to) qs.set('to', to);
  if (matchType !== 'exact') qs.set('matchType', matchType);
  if (filter) {
    for (const f of filter) qs.append('filter', f);
  }

  const res = await archiveGet(`${CDX_API_URL}?${qs}`, 60_000);
  if (res.status < 200 || res.status >= 300) {
    throw new Error(`Wayback CDX API error: ${res.status}`);
  }

  const data: string[][] = JSON.parse(res.body);
  if (!data || data.length <= 1) return [];

  return data.slice(1).map((row) => {
    const timestamp = row[0] ?? '';
    const original = row[1] ?? '';
    const mimetype = row[2] ?? '';
    const statusCode = row[3] ?? '';
    const digest = row[4] ?? '';
    const length = row[5] ?? '';
    return {
      timestamp,
      original,
      mimetype,
      statusCode,
      digest,
      length,
      archiveUrl: `${WAYBACK_BASE_URL}/${timestamp}/${original}`,
      formattedDate: formatTimestamp(timestamp),
    };
  });
}

export async function getArchivedPage(params: {
  url: string;
  timestamp: string;
  original?: boolean;
}): Promise<{ waybackUrl: string; content: string }> {
  const { url, timestamp, original = false } = params;
  const prefix = original ? 'id_' : '';
  const waybackUrl = `${WAYBACK_BASE_URL}/${prefix}${timestamp}/${url}`;

  // Archived pages are static HTML, so plain HTTP beats a browser nav. Links
  // resolve against the final URL (wayback 302s to the canonical timestamp).
  const res = await archiveGet(waybackUrl, 90_000);
  if (res.status < 200 || res.status >= 300) {
    throw new Error(`Wayback fetch error: ${res.status}`);
  }
  const { markdown: content } = await renderMarkdown({ html: res.body, url: res.url, filter: 'raw' });

  return { waybackUrl, content };
}

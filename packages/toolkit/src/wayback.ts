import { scraplingRaw, scraplingRenderMarkdown } from './scrapling.js';
import type { SnapshotInfo } from './types.js';

const CDX_API_URL = 'https://web.archive.org/cdx/search/cdx';
const WAYBACK_BASE_URL = 'https://web.archive.org/web';

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

  // Plain HTTP through the sidecar, never from this process: web.archive.org
  // silently drops this project's datacenter egress (the connection hangs), so
  // every wayback call has to leave on the residential exit — the sidecar
  // picks that by host (STEALTH_HOSTS), so no mode is passed here. See AGENTS.md.
  const res = await scraplingRaw({ url: `${CDX_API_URL}?${qs}` });
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

  // Same egress story as getSnapshots (residential exit, chosen by host).
  // Archived pages are static HTML, so plain HTTP beats a browser nav here —
  // and the render is local either way. Redirects matter: wayback 302s to the
  // canonical timestamp, so links resolve against res.url, not the request.
  const res = await scraplingRaw({ url: waybackUrl, timeoutMs: 90_000 });
  if (res.status < 200 || res.status >= 300) {
    throw new Error(`Wayback fetch error: ${res.status}`);
  }
  const content = await scraplingRenderMarkdown({
    html: res.body,
    url: res.url,
    filter: 'raw',
  });

  return { waybackUrl, content };
}

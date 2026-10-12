// Yahoo web search, fetched by our own stealth browser rather than SearXNG.
//
// Why Yahoo, and why not through SearXNG (measured 2026-10-12 from this
// deployment, `site:linkedin.com/in "Eddie Bauer"`):
//
//   google   SearXNG: "access denied"; direct: a JS-required shell, or /sorry 429
//   brave    429 on every request, through the proxy or not
//   bing     answers, but ignores `site:` for brand queries (0 profiles)
//   mojeek, ecosia, duckduckgo   403 / 202 challenge
//   yahoo    raw HTTP: 500.  Stealth browser: 200, 7 profiles a page, page 2
//            returns new people, and role words narrow it as asked
//
// So Yahoo has to go through Scrapling's stealth browser (Patchright Chromium on
// the US residential exit). Each result also carries LinkedIn's own facts
// ("Works For", "Location", "Occupation"), which are rewritten into the
// "Experience: X · Location: Y" snippet shape Google used, so consumers that
// parse employer and location from snippets keep working unchanged.
import domino from '@mixmark-io/domino';
import { scraplingFetch } from './scrapling.js';

export type YahooResult = { url: string; title: string; content: string; engine: 'yahoo'; engines: ['yahoo'] };

export class YahooError extends Error {}

const RESULTS_PER_PAGE = 10;

export function yahooSearchUrl(query: string, page = 1): string {
  const params = new URLSearchParams({ p: query, n: String(RESULTS_PER_PAGE) });
  if (page > 1) params.set('b', String((page - 1) * RESULTS_PER_PAGE + 1));
  return `https://search.yahoo.com/search?${params}`;
}

/** Yahoo wraps some result links as r.search.yahoo.com/.../RU=<encoded url>/RK=... */
export function unwrapYahooLink(href: string): string {
  const m = href.match(/\/RU=([^/]+)\//);
  if (!m) return href;
  try {
    return decodeURIComponent(m[1]!);
  } catch {
    return href;
  }
}

const text = (el: { textContent: string | null } | null | undefined) =>
  (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

// Yahoo's labels → the snippet vocabulary consumers already parse.
const FACT_LABELS: Record<string, string> = {
  'works for': 'Experience',
  occupation: 'Occupation',
  education: 'Education',
  location: 'Location',
};

export function parseYahooSerp(html: string): YahooResult[] {
  const doc = domino.createDocument(html);
  const out: YahooResult[] = [];
  const seen = new Set<string>();
  for (const block of Array.from(doc.querySelectorAll('div.algo')) as Element[]) {
    const link = block.querySelector('.compTitle a[href]');
    const href = link?.getAttribute('href');
    if (!href) continue;
    const url = unwrapYahooLink(href);
    if (!/^https?:\/\//.test(url) || seen.has(url)) continue;
    // The title sits in an <h3>; the <a> also holds the favicon and breadcrumb.
    const title = text(block.querySelector('h3')) || text(link);
    if (!title) continue;
    const snippet = text(block.querySelector('.compText'));
    const facts: string[] = [];
    let connections: string | null = null;
    for (const li of Array.from(block.querySelectorAll('.compInfo li')) as Element[]) {
      const label = text(li.querySelector('label')).replace(/:$/, '').toLowerCase();
      const value = text(li).replace(/^[^:]*:\s*/, '');
      if (!value) continue;
      if (label === 'connections') connections = value;
      else if (FACT_LABELS[label]) facts.push(`${FACT_LABELS[label]}: ${value}`);
    }
    if (connections) facts.push(`${connections} connections`);
    const content = [facts.join(' · '), snippet].filter(Boolean).join(' · ');
    seen.add(url);
    out.push({ url, title, content, engine: 'yahoo', engines: ['yahoo'] });
  }
  return out;
}

export async function fetchYahoo(query: string, page = 1): Promise<YahooResult[]> {
  const res = await scraplingFetch({ url: yahooSearchUrl(query, page), mode: 'stealth', timeoutMs: 40_000, waitMs: 500 });
  if (res.status !== 200) throw new YahooError(`yahoo: HTTP ${res.status}`);
  const results = parseYahooSerp(res.html);
  // A real results page is titled "<query> - Yahoo Search Results", empty or
  // not (an empty one shows only related searches). A 200 without that title
  // is an interstitial or a changed page, not an empty answer.
  if (results.length === 0 && !/<title>[^<]*Yahoo Search Results/i.test(res.html)) {
    throw new YahooError('yahoo: no result blocks on the page (layout changed or interstitial)');
  }
  return results;
}

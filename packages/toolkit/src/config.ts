import { z } from 'zod';

const envSchema = z.object({
  SEARXNG_URL: z.string().default('http://searxng.railway.internal:8080'),
  SEARXNG_ENGINES: z.string().optional(),
  SEARXNG_CATEGORIES: z.string().optional(),
  API_KEY: z.string().min(1, 'API_KEY is required'),
  SCRAPLING_URL: z.string().default('http://scrapling.railway.internal:8000'),
  CAMOUFOX_URL: z.string().default('http://camoufox.railway.internal:8000'),
});
// No PROXY_* here and never will be: proxy credentials belong to the sidecars
// that own the egress (services/scrapling, services/camoufox), not to the
// process that routes requests. CRAWL4AI_* left with the service — see AGENTS.md.

const env = envSchema.parse(process.env);

export const Config = {
  apiKey: env.API_KEY,
  searxng: {
    url: env.SEARXNG_URL,
    engines: env.SEARXNG_ENGINES,
    categories: env.SEARXNG_CATEGORIES,
  },
  // Owns the fetch/markdown/capture pipeline + residential egress + JS-challenge
  // solving. See services/scrapling.
  scrapling: {
    url: env.SCRAPLING_URL,
  },
  // Stealth Firefox on an ITALIAN residential exit, plus the things no other
  // backend has: a binary fetch through that exit, and a warmed-session
  // in-page fetch for Akamai-gated POSTs. See services/camoufox.
  camoufox: {
    url: env.CAMOUFOX_URL,
  },
  // One request, not three. The three parallel attempts were identical
  // queries hitting the same upstream engines through the same SearXNG, so
  // they could not produce a different answer — they only tripled load and
  // helped burn Brave's rate limit ("too many requests").
  parallelRequests: 1,
  // Must stay comfortably ABOVE SearXNG's own `outgoing.request_timeout`
  // (15s in services/searxng/settings.yml). At 15 it raced SearXNG exactly:
  // responses landed at ~15.13s, the client aborted at 15.00s, and every
  // web_search returned []. Keep this above SearXNG's `max_request_timeout`.
  requestTimeout: 25,
} as const;

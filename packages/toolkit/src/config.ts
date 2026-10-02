import { z } from 'zod';

const envSchema = z.object({
  SEARXNG_URL: z.string().default('http://searxng.railway.internal:8080'),
  SEARXNG_ENGINES: z.string().optional(),
  SEARXNG_CATEGORIES: z.string().optional(),
  API_KEY: z.string().min(1, 'API_KEY is required'),
  SCRAPLING_URL: z.string().default('http://scrapling.railway.internal:8000'),
  CAMOUFOX_URL: z.string().default('http://camoufox.railway.internal:8000'),
  // Score oracle (packages/api/src/oracle.ts): served on THIS service's public
  // domain (the one registered on the reCAPTCHA key).
  RECAPTCHA_ORACLE_URL: z.string().url().optional(),
  RAILWAY_PUBLIC_DOMAIN: z.string().optional(),
});
// No PROXY_* here and never will be: proxy credentials belong to the sidecars
// that own the egress (services/scrapling, services/camoufox), not to the
// process that routes requests.

const env = envSchema.parse(process.env);

export const Config = {
  apiKey: env.API_KEY,
  searxng: {
    url: env.SEARXNG_URL,
    engines: env.SEARXNG_ENGINES,
    categories: env.SEARXNG_CATEGORIES,
  },
  scrapling: {
    url: env.SCRAPLING_URL,
  },
  camoufox: {
    url: env.CAMOUFOX_URL,
  },
  // Where the form browser finds the score oracle (via its residential exit).
  oracleUrl:
    env.RECAPTCHA_ORACLE_URL ??
    (env.RAILWAY_PUBLIC_DOMAIN ? `https://${env.RAILWAY_PUBLIC_DOMAIN}/oracle/recaptcha` : null),
  // Must stay comfortably ABOVE SearXNG's own `outgoing.request_timeout`
  // (15s in services/searxng/settings.yml). At 15 it raced SearXNG exactly:
  // responses landed at ~15.13s, the client aborted at 15.00s, and every
  // web_search returned []. Keep this above SearXNG's `max_request_timeout`.
  requestTimeout: 25,
} as const;

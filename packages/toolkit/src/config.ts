import { z } from 'zod';

const envSchema = z.object({
  SEARXNG_URL: z.string().default('http://searxng.railway.internal:8080'),
  SEARXNG_ENGINES: z.string().optional(),
  SEARXNG_CATEGORIES: z.string().optional(),
  API_KEY: z.string().min(1, 'API_KEY is required'),
  CRAWL4AI_URL: z.string().default('http://crawl4ai.railway.internal:11235'),
  CRAWL4AI_API_TOKEN: z.string().optional(),
  PROXY_SERVER: z.string().optional(),
  PROXY_USERNAME: z.string().optional(),
  PROXY_PASSWORD: z.string().optional(),
});

const env = envSchema.parse(process.env);

export const Config = {
  apiKey: env.API_KEY,
  searxng: {
    url: env.SEARXNG_URL,
    engines: env.SEARXNG_ENGINES,
    categories: env.SEARXNG_CATEGORIES,
  },
  crawl4ai: {
    url: env.CRAWL4AI_URL,
    apiToken: env.CRAWL4AI_API_TOKEN,
  },
  proxy:
    env.PROXY_SERVER && env.PROXY_USERNAME
      ? {
          server: env.PROXY_SERVER,
          username: env.PROXY_USERNAME,
          password: env.PROXY_PASSWORD ?? '',
        }
      : null,
  parallelRequests: 3,
  requestTimeout: 15,
} as const;

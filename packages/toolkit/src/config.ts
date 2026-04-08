import { z } from 'zod';

const envSchema = z.object({
  SEARXNG_URL: z.string().default('http://searxng.railway.internal:8080'),
  SEARXNG_ENGINES: z.string().optional(),
  SEARXNG_CATEGORIES: z.string().optional(),
  API_KEY: z.string().min(1, 'API_KEY is required'),
  CRAWL4AI_URL: z.string().default('http://crawl4ai.railway.internal:11235'),
  CRAWL4AI_API_TOKEN: z.string().optional(),
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
  parallelRequests: 3,
  requestTimeout: 15,
} as const;

import {
  WebSearchInput,
  WebFetchInput,
  WebScreenshotInput,
  WebPdfInput,
  WebExecuteJsInput,
  WebCrawlInput,
  WebSnapshotsInput,
  WebArchiveInput,
} from './schemas.js';
import type { ToolDefinition } from './types.js';

export const tools: ToolDefinition[] = [
  {
    name: 'web_search',
    description: 'Search the web via SearXNG and return results.',
    parameters: WebSearchInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_fetch',
    description: 'Fetch a URL and return its content as clean markdown via Crawl4AI',
    parameters: WebFetchInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_screenshot',
    description: 'Capture a full-page PNG screenshot of a URL via Crawl4AI',
    parameters: WebScreenshotInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_pdf',
    description: 'Generate a PDF document of a URL via Crawl4AI',
    parameters: WebPdfInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_execute_js',
    description: 'Execute JavaScript snippets on a URL via Crawl4AI and return the crawl result',
    parameters: WebExecuteJsInput,
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: true,
    },
  },
  {
    name: 'web_crawl',
    description: 'Crawl one or more URLs and extract their content using Crawl4AI',
    parameters: WebCrawlInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_snapshots',
    description: 'List Wayback Machine snapshots for a URL',
    parameters: WebSnapshotsInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  {
    name: 'web_archive',
    description: 'Retrieve an archived page from the Wayback Machine',
    parameters: WebArchiveInput,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
];

export const toolsByName = new Map(tools.map((t) => [t.name, t]));

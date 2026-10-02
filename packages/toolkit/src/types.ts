import type { z } from 'zod';

// ── Tool system types ────────────────────────────────────────────────

/** Every tool, once. Stats, the tool list and the function map key off this. */
export const TOOL_NAMES = [
  'web_search',
  'web_fetch',
  'web_html',
  'web_screenshot',
  'web_pdf',
  'web_execute_js',
  'web_crawl',
  'web_snapshots',
  'web_archive',
  'web_bytes',
  'web_form_submit',
  'web_form_inspect',
  'web_eval',
  'web_spa_fetch',
  'web_recycle',
  'web_usage_stats',
  'web_agent',
] as const;

export type ToolName = (typeof TOOL_NAMES)[number];

export type ToolAnnotations = {
  readOnlyHint: boolean;
  destructiveHint: boolean;
  idempotentHint: boolean;
  openWorldHint: boolean;
};

export type ToolDefinition = {
  name: ToolName;
  description: string;
  parameters: z.ZodObject<any>;
  annotations: ToolAnnotations;
  /** 'data': REST answers with the bare JSON payload (`ToolResult.data`), 500 on error. */
  output?: 'data';
};

// ── Results ──────────────────────────────────────────────────────────

export type TextContent = { type: 'text'; text: string };
export type ImageContent = { type: 'image'; data: string; mimeType: string };
export type ResourceContent = {
  type: 'resource';
  resource: { uri: string; mimeType: string; blob: string };
};
export type ContentBlock = TextContent | ImageContent | ResourceContent;

/**
 * What every tool returns: the MCP CallToolResult shape.
 *
 * `data` is set by tools whose natural output is JSON (search, snapshots,
 * archive, usage stats). MCP sends it as JSON text in `content` (that block is
 * already there); REST returns `data` bare — the v0 contract those callers
 * parse. The MCP layer strips it.
 */
export type ToolResult = {
  content: ContentBlock[];
  isError?: boolean;
  data?: unknown;
};

// ── Domain types ─────────────────────────────────────────────────────

export type SearchResult = {
  url: string;
  title: string;
  description: string;
};

export type SnapshotInfo = {
  timestamp: string;
  original: string;
  mimetype: string;
  statusCode: string;
  digest: string;
  length: string;
  archiveUrl: string;
  formattedDate: string;
};

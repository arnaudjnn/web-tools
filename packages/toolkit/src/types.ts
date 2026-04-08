import type { z } from 'zod';

// ── Tool system types ────────────────────────────────────────────────

export type ToolAnnotations = {
  readOnlyHint: boolean;
  destructiveHint: boolean;
  idempotentHint: boolean;
  openWorldHint: boolean;
};

export type ToolDefinition = {
  name: string;
  description: string;
  parameters: z.ZodObject<any>;
  annotations: ToolAnnotations;
};

// ── MCP result types ─────────────────────────────────────────────────

export type ToolResult = {
  content: { type: 'text'; text: string }[];
  isError?: boolean;
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

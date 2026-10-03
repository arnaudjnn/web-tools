import type { Request, Response } from 'express';
import { functionMap, validateParams, type ContentBlock, type ToolResult } from '@web-tools/toolkit';
import { respondWithHeartbeat } from './heartbeat.js';

/**
 * The REST v0 body for a tool result. Backward compatible with what REST
 * callers already parse:
 *   - JSON-native tools (output:'data') answer with the bare payload, and
 *     HTTP 500 {error} on failure (HTTP 200 once the heartbeat has committed,
 *     see heartbeat.ts — the {error} body is the signal);
 *   - everything else answers with {content, isError}, where image and PDF
 *     blocks come back as the base64 text they always were.
 */
function restText(c: ContentBlock): { type: 'text'; text: string } {
  if (c.type === 'image') return { type: 'text', text: c.data };
  if (c.type === 'resource') return { type: 'text', text: c.resource.blob };
  return c;
}

export async function runRest(name: string, body: unknown): Promise<{ status: number; body: unknown }> {
  const v = validateParams(name, body);
  if (!v.ok) return { status: v.status, body: { error: v.error, ...(v.issues ? { issues: v.issues } : {}) } };

  const result: ToolResult = await functionMap[v.tool.name](v.params);
  if (v.tool.output === 'data') {
    if (result.isError) return { status: 500, body: { error: result.content.map(restText).map((c) => c.text).join('\n') } };
    return { status: 200, body: result.data };
  }
  return { status: 200, body: { content: result.content.map(restText), isError: result.isError ?? false } };
}

export function toolHandler(name: string) {
  return async (req: Request, res: Response) => {
    // Under HEARTBEAT_MS: today's status codes. Past it: 200 + whitespace
    // heartbeat, and the body (unchanged shape) carries the outcome.
    await respondWithHeartbeat(res, () => runRest(name, req.body));
  };
}

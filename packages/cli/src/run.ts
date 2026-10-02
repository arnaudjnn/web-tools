import { functionMap, validateParams, type ContentBlock, type ToolResult } from '@web-tools/toolkit';

/** Base64 for binary blocks (screenshots, PDFs), the text otherwise. */
const blockText = (c: ContentBlock): string =>
  c.type === 'text' ? c.text : c.type === 'image' ? c.data : c.resource.blob;

/** Validate like REST and MCP do, run the tool, exit 1 on any error. */
export async function runTool(name: string, params: Record<string, unknown>): Promise<ToolResult> {
  const v = validateParams(name, params);
  if (!v.ok) {
    console.error(`${v.error}${v.issues ? `\n${JSON.stringify(v.issues, null, 2)}` : ''}`);
    process.exit(1);
  }
  const result = await functionMap[v.tool.name](v.params);
  if (result.isError) {
    console.error(result.content.map(blockText).join('\n') || 'Unknown error');
    process.exit(1);
  }
  return result;
}

export function printResult(result: ToolResult): void {
  for (const c of result.content) console.log(blockText(c));
}

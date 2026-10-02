import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { functionMap, tools } from '@web-tools/toolkit';

export function createServer(): McpServer {
  const server = new McpServer({ name: 'web_tools', version: '1.0.0' }, { capabilities: { logging: {} } });

  for (const tool of tools) {
    const handler = functionMap[tool.name];
    server.registerTool(
      tool.name,
      { description: tool.description, inputSchema: tool.parameters.shape, annotations: tool.annotations },
      // The SDK has validated params against the same schema REST uses. The
      // handler never throws; `data` is REST-only (its JSON is already in content).
      async (params: Record<string, unknown>) => {
        const { content, isError } = await handler(params);
        return { content, isError: isError ?? false };
      },
    );
  }
  return server;
}

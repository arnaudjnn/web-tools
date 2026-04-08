import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express, { Request, Response } from 'express';
import { Config, tools } from '@web-tools/toolkit';
import { createServer } from './mcp.js';
import { toolHandler } from './handler.js';

const log = (...args: unknown[]) => {
  process.stderr.write(
    args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n',
  );
};

log('Environment check:', { searxngUrl: Config.searxng.url });

const app = express();
app.use(express.json());

// ── Auth middleware (skips /health) ──────────────────────────────────

app.use((req: Request, res: Response, next) => {
  if (req.path === '/health') return next();

  const provided =
    req.headers.authorization?.replace(/^Bearer\s+/i, '') ||
    (req.query.api_key as string);

  if (provided !== Config.apiKey) {
    res.status(403).json({
      error: 'forbidden',
      error_description: 'Invalid or missing API key',
    });
    return;
  }

  next();
});

// ── MCP endpoint ────────────────────────────────────────────────────

app.post('/mcp', async (req: Request, res: Response) => {
  const server = createServer();
  try {
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);

    res.on('close', () => {
      log('Request closed');
      transport.close();
      server.close();
    });
  } catch (error) {
    log('Error handling MCP request:', error);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: '2.0',
        error: { code: -32603, message: 'Internal server error' },
        id: null,
      });
    }
  }
});

app.get('/mcp', async (_req: Request, res: Response) => {
  res.writeHead(405).end(
    JSON.stringify({
      jsonrpc: '2.0',
      error: { code: -32000, message: 'Method not allowed.' },
      id: null,
    }),
  );
});

app.delete('/mcp', async (_req: Request, res: Response) => {
  res.writeHead(405).end(
    JSON.stringify({
      jsonrpc: '2.0',
      error: { code: -32000, message: 'Method not allowed.' },
      id: null,
    }),
  );
});

// ── REST API v0 ─────────────────────────────────────────────────────

app.get('/api/v0', (_req: Request, res: Response) => {
  res.json({
    tools: tools.map((t) => ({
      name: t.name,
      description: t.description,
    })),
  });
});

for (const tool of tools) {
  app.post(`/api/v0/${tool.name}`, toolHandler(tool.name));
}

// ── Health ───────────────────────────────────────────────────────────

app.get('/health', (_req: Request, res: Response) => {
  res.json({ status: 'ok' });
});

// ── Start ────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT || '3000', 10);
app.listen(PORT, () => {
  log(`Web Tools server listening on port ${PORT}`);
  log(`  MCP:    POST /mcp`);
  log(`  API:    POST /api/v0/{tool_name}`);
  log(`  Health: GET  /health`);
});

process.on('SIGINT', async () => {
  log('Shutting down server...');
  process.exit(0);
});

import { createHash, timingSafeEqual } from 'node:crypto';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express, { Request, Response } from 'express';
import { Config, getStats, log, tools } from '@web-tools/toolkit';
import { createServer } from './mcp.js';
import { toolHandler } from './handler.js';
import { mountOracle } from './oracle.js';

log('Environment check:', { searxngUrl: Config.searxng.url });

const app = express();
app.use(express.json());

// Set on SIGTERM. Keep-alive clients are told to reconnect elsewhere.
let draining = false;
app.use((_req: Request, res: Response, next) => {
  if (draining) res.set('Connection', 'close');
  next();
});

// ── Score oracle (BEFORE auth: the form browser carries no API key) ──
// GET/POST /oracle/recaptcha — 404 unless RECAPTCHA_ORACLE_SITEKEY/SECRET are
// set. See oracle.ts for why Tools (the public hostname) serves it.
mountOracle(app, log);

// ── Auth middleware (skips /health) ──────────────────────────────────

// Constant-time: compare fixed-length digests, so neither the content nor the
// length of the key leaks through response timing.
const digest = (s: string) => createHash('sha256').update(s).digest();
const expectedKey = digest(Config.apiKey);
const keyMatches = (provided: unknown) =>
  typeof provided === 'string' && provided.length > 0 && timingSafeEqual(digest(provided), expectedKey);

app.use((req: Request, res: Response, next) => {
  if (req.path === '/health') return next();

  const provided =
    req.headers.authorization?.replace(/^Bearer\s+/i, '') ||
    (req.query.api_key as string);

  if (!keyMatches(provided)) {
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
  if (draining) {
    res.status(503).json({ status: 'draining' });
    return;
  }
  res.json({ status: 'ok' });
});

// ── Stats / cost monitoring ─────────────────────────────────────────
// Process-local counters. In-memory; resets on container restart
// (started_at reveals the reset). Same shape as the web_usage_stats
// MCP tool — a plain GET so dashboards / cron can poll cheaply.
app.get('/stats', (_req: Request, res: Response) => {
  res.json(getStats());
});

// ── Start ────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT || '3000', 10);
const server = app.listen(PORT, () => {
  log(`Web Tools server listening on port ${PORT}`);
  log(`  MCP:    POST /mcp`);
  log(`  API:    POST /api/v0/{tool_name}`);
  log(`  Health: GET  /health`);
});

// Graceful drain: stop accepting, let in-flight calls finish (a crawl or a
// form submission cut mid-flight is a lost or unknown result), then exit.
// Bounded, because the platform SIGKILLs eventually anyway — set Railway's
// RAILWAY_DEPLOYMENT_DRAINING_SECONDS at or above DRAIN_TIMEOUT_MS/1000.
const DRAIN_TIMEOUT_MS = Number(process.env.DRAIN_TIMEOUT_MS ?? '60000');

function shutdown(signal: string): void {
  if (draining) return;
  draining = true;
  log(`${signal}: draining (up to ${DRAIN_TIMEOUT_MS / 1000}s)...`);
  server.close(() => {
    log('drained; exiting');
    process.exit(0);
  });
  server.closeIdleConnections();
  setTimeout(() => {
    log('drain timeout; exiting with requests in flight');
    process.exit(1);
  }, DRAIN_TIMEOUT_MS).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

// The whitespace heartbeat on a real Express app on loopback, with a short
// interval and fake slow work — no fake timers, so the bytes really cross a socket.
import type { AddressInfo } from 'node:net';
import { request } from 'node:http';
import express from 'express';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { activeHeartbeats, heartbeatMs, respondWithHeartbeat, type RestOutcome } from '../src/heartbeat.js';

const MS = 40;
let base = '';
let close: () => void;

// Each test parks its work here; the route awaits it.
let work: () => Promise<RestOutcome> = async () => ({ status: 200, body: {} });
let afterResponse: Promise<void> = Promise.resolve();

beforeAll(async () => {
  const app = express();
  app.post('/t', async (_req, res) => {
    const done = respondWithHeartbeat(res, () => work(), MS);
    afterResponse = done;
    await done;
  });
  const server = app.listen(0, '127.0.0.1');
  await new Promise((r) => server.once('listening', r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  close = () => server.close();
});
afterAll(() => close());

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const slow = (ms: number, outcome: RestOutcome) => async () => (await sleep(ms), outcome);

describe('REST heartbeat', () => {
  it('a fast call keeps its exact status code and body', async () => {
    work = slow(0, { status: 400, body: { error: 'invalid_params', issues: [] } });
    const r = await fetch(`${base}/t`, { method: 'POST' });
    expect(r.status).toBe(400);
    expect(r.headers.get('content-type')).toMatch(/^application\/json/);
    expect(r.headers.get('x-accel-buffering')).toBeNull();
    expect(await r.text()).toBe('{"error":"invalid_params","issues":[]}');
    expect(activeHeartbeats()).toBe(0);
  });

  it('a slow call commits 200, streams spaces, then a body that parses', async () => {
    work = slow(MS * 3.5, { status: 200, body: { content: [{ type: 'text', text: 'done' }], isError: false } });
    const r = await fetch(`${base}/t`, { method: 'POST' });
    // Headers arrive after the first beat, long before the result.
    expect(r.status).toBe(200);
    expect(r.headers.get('content-type')).toMatch(/^application\/json/);
    expect(r.headers.get('cache-control')).toBe('no-store');
    expect(r.headers.get('x-accel-buffering')).toBe('no');
    const text = await r.text();
    expect(text).toMatch(/^ {2,}\{/);
    expect(JSON.parse(text)).toEqual({ content: [{ type: 'text', text: 'done' }], isError: false });
    expect(activeHeartbeats()).toBe(0);
  });

  it('past the grace period a failure is still 200, and the body carries the error', async () => {
    work = slow(MS * 2.5, { status: 500, body: { error: 'searxng HTTP 502' } });
    const r = await fetch(`${base}/t`, { method: 'POST' });
    expect(r.status).toBe(200);
    expect(JSON.parse(await r.text())).toEqual({ error: 'searxng HTTP 502' });
  });

  it('a throw after commit becomes {error} in the body', async () => {
    work = async () => {
      await sleep(MS * 2.5);
      throw new Error('boom');
    };
    const r = await fetch(`${base}/t`, { method: 'POST' });
    expect(r.status).toBe(200);
    expect(JSON.parse(await r.text())).toEqual({ error: 'boom' });
  });

  it('a throw before commit still reaches the Express error handler (500)', async () => {
    work = async () => {
      throw new Error('fast boom');
    };
    const r = await fetch(`${base}/t`, { method: 'POST' });
    expect(r.status).toBe(500);
    expect(activeHeartbeats()).toBe(0);
  });

  it('a client disconnect stops the heartbeat and the late result does not crash', async () => {
    let finish!: (o: RestOutcome) => void;
    work = () => new Promise<RestOutcome>((r) => (finish = r));
    const req = request(`${base}/t`, { method: 'POST' });
    const gotHeaders = new Promise<void>((r) => req.on('response', () => r()));
    req.on('error', () => {});
    req.end();
    await gotHeaders; // committed: the heartbeat is running
    expect(activeHeartbeats()).toBe(1);
    req.destroy();
    await vi.waitFor(() => expect(activeHeartbeats()).toBe(0));
    finish({ status: 200, body: { late: true } });
    await expect(afterResponse).resolves.toBeUndefined();
  });
});

describe('heartbeatMs', () => {
  it('defaults to 20 s and honours a positive HEARTBEAT_MS', () => {
    vi.stubEnv('HEARTBEAT_MS', '');
    expect(heartbeatMs()).toBe(20_000);
    vi.stubEnv('HEARTBEAT_MS', 'nope');
    expect(heartbeatMs()).toBe(20_000);
    vi.stubEnv('HEARTBEAT_MS', '25000');
    expect(heartbeatMs()).toBe(25_000);
    vi.unstubAllEnvs();
  });
});

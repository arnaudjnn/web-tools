import { afterEach, describe, expect, it } from 'vitest';
import { getJob, resetFormJobs, siteLoad, siteOf, startJob, withSiteLimit } from '../src/form_jobs.js';
import type { ToolResult } from '../src/types.js';

afterEach(() => {
  resetFormJobs();
  delete process.env.FORM_SITE_CONCURRENCY;
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const ok = (body: unknown): ToolResult => ({ content: [{ type: 'text', text: JSON.stringify(body) }], isError: false });

describe('siteOf', () => {
  it('keys a form by its hostname without www', () => {
    expect(siteOf('https://www.Atoka.io/en/try-atoka/')).toBe('atoka.io');
    expect(siteOf('https://forms.example.org/a')).toBe('forms.example.org');
  });
});

describe('withSiteLimit', () => {
  it('runs at most FORM_SITE_CONCURRENCY per site, and other sites never wait', async () => {
    process.env.FORM_SITE_CONCURRENCY = '2';
    let peak = 0;
    let active = 0;
    const job = async () => {
      active += 1;
      peak = Math.max(peak, active);
      await sleep(20);
      active -= 1;
    };
    const runs = [1, 2, 3, 4].map(() => withSiteLimit('https://a.test/f', job));
    await sleep(5);
    expect(siteLoad('https://a.test/f')).toEqual({ active: 2, waiting: 2 });
    let otherRan = false;
    await withSiteLimit('https://b.test/f', async () => {
      otherRan = true;
    });
    expect(otherRan).toBe(true);
    await Promise.all(runs);
    expect(peak).toBe(2);
    expect(siteLoad('https://a.test/f')).toEqual({ active: 0, waiting: 0 });
  });

  it('frees the slot when the run throws', async () => {
    process.env.FORM_SITE_CONCURRENCY = '1';
    await expect(withSiteLimit('https://a.test', async () => { throw new Error('x'); })).rejects.toThrow('x');
    await withSiteLimit('https://a.test', async () => undefined); // not stuck
  });
});

describe('async jobs', () => {
  it('goes queued/running -> done with the result', async () => {
    const job = startJob('https://a.test/f', async () => {
      await sleep(10);
      return ok({ ok: true });
    });
    expect(['queued', 'running']).toContain(job.status);
    await sleep(30);
    const done = getJob(job.job_id)!;
    expect(done.status).toBe('done');
    expect(done.result?.isError).toBe(false);
    expect(done.finished_at).toBeTruthy();
  });

  it('turns a thrown run into an unknown outcome, never a crash', async () => {
    const job = startJob('https://a.test/f', async () => {
      throw new Error('boom');
    });
    await sleep(10);
    const done = getJob(job.job_id)!;
    expect(done.status).toBe('done');
    expect(done.result?.isError).toBe(true);
    expect(JSON.stringify(done.result)).toContain('unknown');
  });

  it('returns undefined for an unknown id', () => {
    expect(getJob('nope')).toBeUndefined();
  });
});

// Form submissions for agents: a per-site concurrency limit and async jobs.
//
// Per site: a score-based CAPTCHA (reCAPTCHA v3 on Atoka) scores the target's
// own traffic, and acceptance fell as our parallel submissions to ONE site
// rose (2026-10-05: 1-2 in flight 64-83% per attempt, 4 in flight 50%). So at
// most FORM_SITE_CONCURRENCY (default 2) submissions run per site; the rest
// wait their turn. Different sites never wait on each other.
//
// Async: a form takes ~10-60 s. `async: true` returns a job id at once and
// web_form_result polls it, so an agent can queue many forms without holding
// a request open. Jobs live in this process's memory for JOB_TTL_MS — a Tools
// restart forgets them (the submission itself may still have happened, so a
// lost job is an unknown outcome, never a replay signal).

import { randomUUID } from 'node:crypto';
import type { ToolResult } from './types.js';

const JOB_TTL_MS = 60 * 60 * 1000;
const MAX_JOBS = 2000;

function siteLimit(): number {
  const n = Number(process.env.FORM_SITE_CONCURRENCY ?? '2');
  return Number.isFinite(n) && n >= 1 ? Math.floor(n) : 2;
}

/** The site a form belongs to: its hostname without a leading `www.`. */
export function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '').toLowerCase();
  } catch {
    return url;
  }
}

type Gate = { active: number; waiting: Array<() => void> };
const gates = new Map<string, Gate>();

/** Run `fn` once the site has a free slot (at most siteLimit() in flight). */
export async function withSiteLimit<T>(url: string, fn: () => Promise<T>): Promise<T> {
  const site = siteOf(url);
  let gate = gates.get(site);
  if (!gate) {
    gate = { active: 0, waiting: [] };
    gates.set(site, gate);
  }
  const g = gate;
  if (g.active >= siteLimit()) {
    await new Promise<void>((resolve) => g.waiting.push(resolve));
  }
  g.active += 1;
  try {
    return await fn();
  } finally {
    g.active -= 1;
    const next = g.waiting.shift();
    if (next) next();
    else if (g.active === 0) gates.delete(site);
  }
}

/** Submissions running or queued for a site (tests, diagnostics). */
export function siteLoad(url: string): { active: number; waiting: number } {
  const g = gates.get(siteOf(url));
  return { active: g?.active ?? 0, waiting: g?.waiting.length ?? 0 };
}

export type FormJob = {
  job_id: string;
  status: 'queued' | 'running' | 'done';
  site: string;
  created_at: string;
  finished_at?: string;
  result?: ToolResult;
};

const jobs = new Map<string, FormJob & { at: number }>();

function sweep(now = Date.now()): void {
  for (const [id, job] of jobs) {
    if (now - job.at > JOB_TTL_MS) jobs.delete(id);
  }
  while (jobs.size > MAX_JOBS) {
    const oldest = jobs.keys().next().value as string;
    jobs.delete(oldest);
  }
}

/** Start `run` as a background job behind the site limit; returns the job. */
export function startJob(url: string, run: () => Promise<ToolResult>): FormJob {
  sweep();
  const job = {
    job_id: randomUUID(),
    status: 'queued' as FormJob['status'],
    site: siteOf(url),
    created_at: new Date().toISOString(),
    at: Date.now(),
  };
  jobs.set(job.job_id, job);
  void withSiteLimit(url, async () => {
    job.status = 'running';
    try {
      return await run();
    } catch (err) {
      return {
        content: [{ type: 'text', text: JSON.stringify({ ok: false, retryable: false, outcome: 'unknown', error: String(err) }) }],
        isError: true,
      } satisfies ToolResult;
    }
  }).then((result) => {
    Object.assign(job, { status: 'done', result, finished_at: new Date().toISOString() });
  });
  return publicJob(job);
}

export function getJob(id: string): FormJob | undefined {
  const job = jobs.get(id);
  return job ? publicJob(job) : undefined;
}

function publicJob(job: FormJob & { at: number }): FormJob {
  const { at: _at, ...rest } = job;
  return { ...rest };
}

/** Tests only. */
export function resetFormJobs(): void {
  jobs.clear();
  gates.clear();
}

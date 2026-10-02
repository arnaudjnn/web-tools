// The tool registry and the one validator REST, MCP and the CLI share.
import { describe, expect, it } from 'vitest';
import { functionMap } from '../src/functions.js';
import { CRAWL_DEADLINE_MS, MAX_CRAWL_URLS, MAX_FETCH_TIMEOUT_MS } from '../src/schemas.js';
import { isToolName, tools, toolsByName, validateParams } from '../src/tools.js';
import { TOOL_NAMES } from '../src/types.js';

describe('tool registry', () => {
  it('defines every tool exactly once, each with an implementation', () => {
    const names = tools.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
    expect([...names].sort()).toEqual([...TOOL_NAMES].sort());
    expect(Object.keys(functionMap).sort()).toEqual([...TOOL_NAMES].sort());
    expect(toolsByName.size).toBe(TOOL_NAMES.length);
  });

  it('JSON-native tools are exactly search, snapshots, archive and usage stats', () => {
    expect(tools.filter((t) => t.output === 'data').map((t) => t.name).sort()).toEqual(
      ['web_archive', 'web_search', 'web_snapshots', 'web_usage_stats'],
    );
  });

  it('only state-changing tools are marked non-read-only', () => {
    const mutating = tools.filter((t) => !t.annotations.readOnlyHint).map((t) => t.name).sort();
    expect(mutating).toContain('web_form_submit');
    expect(mutating).toContain('web_recycle');
    expect(mutating).not.toContain('web_form_inspect');
    expect(mutating).not.toContain('web_fetch');
    expect(toolsByName.get('web_recycle')!.annotations.destructiveHint).toBe(true);
  });

  it('isToolName', () => {
    expect(isToolName('web_fetch')).toBe(true);
    expect(isToolName('web_nope')).toBe(false);
  });
});

describe('validateParams', () => {
  it('404s an unknown tool', () => {
    expect(validateParams('web_nope', {})).toEqual({ ok: false, status: 404, error: 'Unknown tool: web_nope' });
  });

  it('400s with the zod issues', () => {
    const v = validateParams('web_fetch', { url: 'nope' });
    expect(v).toMatchObject({ ok: false, status: 400, error: 'invalid_params', issues: [{ path: ['url'] }] });
  });

  it('treats a missing body as {} and strips unknown keys', () => {
    expect(validateParams('web_usage_stats', undefined)).toMatchObject({ ok: true, params: {} });
    const v = validateParams('web_fetch', { url: 'https://e.com/', engine: 'camoufox' });
    expect(v).toMatchObject({ ok: true, params: { url: 'https://e.com/' } });
    expect(v.ok && 'engine' in v.params).toBe(false);
  });

  it('enforces the crawl and timeout limits', () => {
    const urls = (n: number) => Array(n).fill('https://e.com/');
    expect(validateParams('web_crawl', { urls: urls(MAX_CRAWL_URLS) }).ok).toBe(true);
    expect(validateParams('web_crawl', { urls: urls(MAX_CRAWL_URLS + 1) })).toMatchObject({ status: 400 });
    expect(validateParams('web_crawl', { urls: [] })).toMatchObject({ status: 400 });
    expect(validateParams('web_html', { url: 'https://e.com/', timeout_ms: MAX_FETCH_TIMEOUT_MS }).ok).toBe(true);
    expect(validateParams('web_html', { url: 'https://e.com/', timeout_ms: MAX_FETCH_TIMEOUT_MS + 1 })).toMatchObject({ status: 400 });
    expect(validateParams('web_fetch', { url: 'https://e.com/', delay: 61 })).toMatchObject({ status: 400 });
    expect(CRAWL_DEADLINE_MS).toBe(300_000);
  });

  it('web_fetch f is raw|fit only', () => {
    expect(validateParams('web_fetch', { url: 'https://e.com/', f: 'raw' }).ok).toBe(true);
    expect(validateParams('web_fetch', { url: 'https://e.com/', f: 'bm25' }).ok).toBe(false);
  });
});

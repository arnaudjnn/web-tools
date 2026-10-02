// HTML → markdown: Scrapling's /markdown first, a local renderer when the
// sidecar cannot answer.
//
// Without the local path a Scrapling outage killed web_fetch, web_crawl and
// web_archive even though Camoufox had fetched the page fine — the render was
// the single point of failure. The local renderer mirrors the sidecar's
// pipeline (services/scrapling/app.py `_render_markdown`) closely enough to be
// a drop-in: same noise and hidden-content strip, same `fit`/`raw` scope, same
// css_selector semantics, links absolutised against <base href> or the final
// URL. turndown + domino (turndown's own parser, so no second DOM library).

import domino from '@mixmark-io/domino';
import TurndownService from 'turndown';
import { errMsg, log } from './log.js';
import { scraplingRenderMarkdown } from './scrapling.js';

export type MarkdownFilter = 'raw' | 'fit';
export type MarkdownRenderer = 'scrapling' | 'local';

export type RenderParams = {
  html: string;
  /** The URL the HTML came from (after redirects); relative links resolve against it. */
  url: string;
  filter?: MarkdownFilter;
  cssSelector?: string;
};

/** Render through the sidecar, falling back to the local renderer. */
export async function renderMarkdown(
  params: RenderParams,
): Promise<{ markdown: string; renderer: MarkdownRenderer }> {
  try {
    return { markdown: await scraplingRenderMarkdown(params), renderer: 'scrapling' };
  } catch (err) {
    log('markdown: scrapling /markdown failed, rendering locally:', errMsg(err));
    try {
      return { markdown: renderMarkdownLocal(params), renderer: 'local' };
    } catch (err2) {
      throw new Error(`scrapling: ${errMsg(err)}; local: ${errMsg(err2)}`);
    }
  }
}

// Scrapling's Convertor: _strip_noise_tags + _sanitize_for_ai.
const NOISE_SELECTOR = 'script, style, noscript, svg, template, [aria-hidden="true"]';
const HIDDEN_STYLE_RE =
  /(display:\s?none|visibility:\s?hidden|opacity:\s?0|font-size:\s?0|height:\s?0|width:\s?0)/i;
const ZERO_WIDTH_RE = /[​‌‍﻿⁠᠎]/g;
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f]/g;
const SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

const turndown = new TurndownService({
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '*',
});

function absolutize(value: string, base: string): string {
  if (!value || SCHEME_RE.test(value)) return value;
  try {
    return new URL(value, base).href;
  } catch {
    return value;
  }
}

/** The local renderer. Pure, synchronous, no network. */
export function renderMarkdownLocal(params: RenderParams): string {
  const doc = domino.createDocument(params.html, true);

  // A document that declares its own <base href> has already chosen the origin
  // its relative links belong to; overriding it would break rebased links.
  const baseHref = doc.querySelector('base[href]')?.getAttribute('href');
  const base = baseHref ? absolutize(baseHref, params.url) : params.url;

  const root: Element =
    (params.filter ?? 'fit') === 'fit' ? (doc.body ?? doc.documentElement) : doc.documentElement;

  for (const el of Array.from(root.querySelectorAll(NOISE_SELECTOR))) el.remove();
  for (const el of Array.from(root.querySelectorAll('[style]'))) {
    if (HIDDEN_STYLE_RE.test(el.getAttribute('style') ?? '')) el.remove();
  }
  for (const el of Array.from(root.querySelectorAll('a[href]'))) {
    el.setAttribute('href', absolutize(el.getAttribute('href') ?? '', base));
  }
  for (const el of Array.from(root.querySelectorAll('img[src]'))) {
    el.setAttribute('src', absolutize(el.getAttribute('src') ?? '', base));
  }

  const parts = params.cssSelector ? Array.from(root.querySelectorAll(params.cssSelector)) : [root];
  return parts
    .map((el) => turndown.turndown(el as unknown as HTMLElement))
    .join('\n\n')
    .replace(ZERO_WIDTH_RE, '')
    .replace(CONTROL_RE, '');
}

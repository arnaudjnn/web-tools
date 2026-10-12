// Yahoo SERP parsing, against a page captured from the stealth browser
// (2026-10-12, `site:linkedin.com/in "Eddie Bauer" ecommerce`).
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { parseYahooSerp, unwrapYahooLink, yahooSearchUrl } from '../src/yahoo.js';

const html = readFileSync(new URL('./fixtures/yahoo-linkedin-serp.html', import.meta.url), 'utf8');

describe('parseYahooSerp', () => {
  const results = parseYahooSerp(html);

  it('reads one result per block, with the LinkedIn profile URL as the link', () => {
    expect(results).toHaveLength(3);
    expect(results[0]!.url).toBe('https://www.linkedin.com/in/kristy-mattson-0bb895');
    expect(results[1]!.url).toBe('https://ca.linkedin.com/in/abhijeet-mishra-1699b920');
    expect(results.every((r) => r.engine === 'yahoo')).toBe(true);
  });

  it('takes the title from the heading, not the favicon or breadcrumb', () => {
    expect(results[0]!.title).toMatch(/^Kristy Mattson - Ecommerce Leadership/);
    expect(results[0]!.title).not.toMatch(/linkedin\.com ›/);
  });

  it('rewrites LinkedIn facts into the Google snippet shape', () => {
    // "Works For" becomes "Experience", which is where Google put the employer.
    expect(results[0]!.content).toMatch(/^Location: Greater Seattle Area · Experience: Eddie Bauer · 500\+ connections · /);
    expect(results[2]!.content).toMatch(/Education: Institute of Leadership & Management/);
  });
});

describe('yahoo links and paging', () => {
  it('unwraps r.search.yahoo.com redirects', () => {
    expect(
      unwrapYahooLink('https://r.search.yahoo.com/_ylt=x/RV=2/RE=1/RO=10/RU=https%3a%2f%2fwww.linkedin.com%2fin%2fjane-doe/RK=2/RS=y-'),
    ).toBe('https://www.linkedin.com/in/jane-doe');
    expect(unwrapYahooLink('https://www.linkedin.com/in/a')).toBe('https://www.linkedin.com/in/a');
  });

  it('pages with b = (page - 1) * 10 + 1', () => {
    expect(new URL(yahooSearchUrl('q')).searchParams.has('b')).toBe(false);
    expect(new URL(yahooSearchUrl('q', 3)).searchParams.get('b')).toBe('21');
  });
});

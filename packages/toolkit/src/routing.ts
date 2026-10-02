// Which backend serves a URL. Decided here, never asked of the caller.
//
// There are two fetchers and they are not interchangeable:
//
//   scrapling  Patchright Chromium: US residential exit (stealth mode), or
//              challenge-solving. Fast mode egresses on this host's own IP.
//   camoufox   Firefox on an ITALIAN residential exit, geoip-coherent.
//
// A caller asking for a URL should not have to know any of that, so the choice
// is made from the host. The rule that matters: an Italian authority site wants
// an Italian visitor. Those sources bot-gate datacenter IPs (Radware/hCaptcha on
// Consob) or score the exit IP's country as part of a sensor decision (Akamai on
// the tributario SPA), and a US residential exit is no better than a datacenter
// one for them — it is the wrong country with extra latency.

export type Backend = 'scrapling' | 'camoufox';

/**
 * Hosts that must go to Camoufox whatever the default routing says.
 *
 * trustpilot.com (measured 2026-09-27): Scrapling fast answers with the 970-byte
 * challenge interstitial, and escalating into the managed-Turnstile solve loop
 * wedged the whole worker for minutes (hence NEVER_ESCALATE_HOSTS in app.py).
 * Camoufox renders the full page from the Italian exit — but only with a
 * forced wait: the review list hydrates well after load.
 */
const CAMOUFOX_HOSTS = ['trustpilot.com'];

/**
 * Hosts that must be fetched as an Italian residential visitor.
 *
 * `.it` covers the bulk of them, and is deliberately broad: for an Italian
 * source the Italian exit is never the *wrong* answer, only sometimes an
 * unnecessarily expensive one. Non-`.it` Italian sources are listed explicitly.
 */
const ITALIAN_SUFFIXES = [
  '.it', // consob.it, ivass.it, giustiziatributaria.gov.it, fiscooggi.it, …
];

const ITALIAN_HOSTS: string[] = [
  // Italian sources that do NOT live under .it, which the suffix rule would
  // therefore miss. This list is the whole reason the suffix rule is not enough:
  // altalex.com is an Italian legal-commentary source behind Cloudflare/SSO, and
  // routing it to a US exit sends the wrong visitor to a site that is gated on
  // being the right one. Add here rather than widening the suffix list, so the
  // blast radius of a new entry is one host.
  'altalex.com',
];

function hostOf(url: string): string | null {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
}

function matchesHost(host: string, patterns: string[]): boolean {
  return patterns.some((p) => host === p || host.endsWith('.' + p));
}

export function isItalianSource(url: string): boolean {
  const host = hostOf(url);
  if (!host) return false;
  if (ITALIAN_SUFFIXES.some((s) => host.endsWith(s))) return true;
  return matchesHost(host, ITALIAN_HOSTS);
}

/**
 * A settle time this host NEEDS, regardless of what the caller asked for.
 *
 * Returns undefined for ordinary hosts (the caller's own wait applies). The
 * forced value is a measured minimum for content that hydrates long after
 * load — see CAMOUFOX_HOSTS.
 */
export function forcedWaitMs(url: string): number | undefined {
  const host = hostOf(url);
  if (host && matchesHost(host, CAMOUFOX_HOSTS)) return 20_000;
  return undefined;
}

/**
 * The fetcher for a URL.
 *
 * Note what is NOT decided here: whether Scrapling uses its proxy or solves a
 * challenge. That is the sidecar's own call (it routes by host and escalates on
 * evidence), and duplicating it here would give us two routing tables to keep in
 * agreement. This function only picks the *service*.
 */
export function pickBackend(url: string): Backend {
  if (isItalianSource(url)) return 'camoufox';
  const host = hostOf(url);
  if (host && matchesHost(host, CAMOUFOX_HOSTS)) return 'camoufox';
  return 'scrapling';
}

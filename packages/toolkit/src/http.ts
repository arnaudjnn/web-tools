// Node's fetch (undici) aborts any request whose response headers take longer
// than 300s (headersTimeout) or whose body stalls 300s (bodyTimeout), and it
// does so silently as "fetch failed". A score-gated wizard form legitimately
// answers after ~360s, so that default turned real submissions into unknown
// outcomes (measured 2026-10-02: Tools gave up at ~300s while Camoufox-Forms
// was still walking the wizard). Every sidecar call already carries its own
// AbortSignal deadline; that deadline is the only one that should apply.
import { Agent, setGlobalDispatcher } from 'undici';

const TEN_MINUTES_MS = 600_000;

setGlobalDispatcher(new Agent({ headersTimeout: TEN_MINUTES_MS, bodyTimeout: TEN_MINUTES_MS }));

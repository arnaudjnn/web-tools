"""Per-request fingerprint knobs for the form browser (`fingerprint` field).

A request may carry `fingerprint: {...}` to steer what Camoufox draws for
that one form browser — and, because the score gate must probe the way the
form loads, for the gate's oracle probes too. Absent (the default), the
launch is byte-for-byte what it was: `geoip=True, humanize=True`, every
other value drawn by Camoufox. This exists to MEASURE coherence hypotheses
(OS, locale, screen/window, WebGL, fonts) against the oracle and the
target, one knob at a time; a knob that wins is promoted to a default on
its own evidence, never by leaving a request field set.

`launch_kwargs(spec)` is the single mapping from the request's spec to
Camoufox launch kwargs (pure: no browser, unit-tested). Keys:

- `os`: "windows" | "macos" | "linux" — the fingerprint's OS (UA,
  platform, fonts, WebGL pool). Default: Camoufox's random draw.
- `locale`: "it-IT" or ["it-IT", "en-US"] — navigator.language(s),
  Accept-Language and the Intl locale. Default: geoip's (the exit's).
- `screen`: [w, h] — pin screen.width/height (a BrowserForge constraint
  with min == max). `window`: [w, h] — the outer window size.
- `fonts`, `custom_fonts_only`: extra font families (and "only these").
- `block_webgl`; `webgl_config`: [vendor, renderer] (needs `os`).
- `humanize`: true/false or the max seconds of a cursor move.
- `fingerprint_preset`: true — a real-device preset instead of a
  synthetic BrowserForge draw.
- `viewport`: "native" (default) | "fixed" (isolated form contexts opened
  with a 1440x900 viewport). "native": no viewport override: Camoufox's own
  no_viewport default, so the page sees the spoofed window — what a
  persistent profile's context already gets). The fixed viewport overrides
  the drawn window: measured on cf156, outerHeight 985 on a 900-high screen,
  and innerWidth 1440 on a 1366-wide macOS screen — geometry no real
  browser reports. Not a launch kwarg: `context_options(spec)` reads it.
- `config`: raw CAMOU_CONFIG overrides (e.g. `window.devicePixelRatio`,
  `headers.Accept-Language`); `firefox_user_prefs`: raw Firefox prefs.

Values are request-scoped measurement inputs: they are logged by NAME only
(`names(spec)` in the form-run line), never echoed back with identity data.
A named persistent `profile` draws its fingerprint ONCE: the spec shapes
that first draw and is ignored on later launches (the saved options win —
the profile's identity must not drift run to run).
"""
from __future__ import annotations

OSES = ("windows", "macos", "linux")
SCALAR = (str, int, float, bool)


def _pair(value, name):
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        raise ValueError("fingerprint.%s must be a [a, b] pair" % name)
    return value


def _dims(value, name):
    w, h = _pair(value, name)
    if not all(isinstance(v, int) and not isinstance(v, bool) and 200 <= v <= 8192 for v in (w, h)):
        raise ValueError("fingerprint.%s must be two integers in 200..8192" % name)
    return int(w), int(h)


def _flat_dict(value, name):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("fingerprint.%s must be an object of at most 64 keys" % name)
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 120:
            raise ValueError("fingerprint.%s keys must be short strings" % name)
        ok = isinstance(item, SCALAR) or (isinstance(item, list) and all(isinstance(i, SCALAR) for i in item))
        if not ok:
            raise ValueError("fingerprint.%s.%s must be a scalar or a list of scalars" % (name, key))
    return dict(value)


KEYS = ("os", "locale", "screen", "window", "fonts", "custom_fonts_only", "block_webgl",
        "webgl_config", "humanize", "fingerprint_preset", "config", "firefox_user_prefs", "viewport")
VIEWPORTS = ("fixed", "native")
FIXED_VIEWPORT = {"width": 1440, "height": 900}


def context_options(spec) -> dict:
    """new_context() kwargs for an isolated form context (service workers are
    the caller's). Default (no spec / "native"): none — Camoufox applies
    no_viewport itself; "fixed": the 1440x900 viewport forms used before."""
    # Default "native": the fixed 1440x900 override showed pages a window
    # larger than their own screen (1440x985 on a 1366x768 draw), which no
    # real browser does and any script can read (lane-fingerprint, 2026-10-03).
    viewport = (spec or {}).get("viewport") or "native"
    if viewport not in VIEWPORTS:
        raise ValueError("fingerprint.viewport must be one of %s" % ", ".join(VIEWPORTS))
    return {} if viewport == "native" else {"viewport": dict(FIXED_VIEWPORT)}


def validate(spec):
    """Raise ValueError on an unusable spec (the endpoint answers 400)."""
    launch_kwargs(spec, screen_factory=lambda **kw: kw)


def names(spec) -> list[str] | None:
    """Which knobs a request set — for the form-run line (never values)."""
    if not spec:
        return None
    keys = sorted(k for k, v in spec.items() if v is not None)
    for raw in ("config", "firefox_user_prefs"):
        if isinstance(spec.get(raw), dict):
            keys += ["%s.%s" % (raw, k) for k in sorted(spec[raw])]
    return keys


def _screen(**constraints):
    from browserforge.fingerprints import Screen
    return Screen(**constraints)


def launch_kwargs(spec, screen_factory=_screen) -> dict:
    """The Camoufox launch kwargs a fingerprint spec adds ({} for none)."""
    if not spec:
        return {}
    if not isinstance(spec, dict):
        raise ValueError("fingerprint must be an object")
    unknown = set(spec) - set(KEYS)
    if unknown:
        raise ValueError("unknown fingerprint keys: %s" % ", ".join(sorted(unknown)))
    out = {}
    context_options(spec)  # validates `viewport` (a context option, not a launch kwarg)
    if spec.get("os") is not None:
        if spec["os"] not in OSES:
            raise ValueError("fingerprint.os must be one of %s" % ", ".join(OSES))
        out["os"] = spec["os"]
    locale = spec.get("locale")
    if locale is not None:
        items = [locale] if isinstance(locale, str) else locale
        if not (isinstance(items, list) and 1 <= len(items) <= 5
                and all(isinstance(i, str) and 2 <= len(i) <= 20 for i in items)):
            raise ValueError("fingerprint.locale must be a locale or a list of up to 5")
        out["locale"] = locale if isinstance(locale, str) else list(items)
    if spec.get("screen") is not None:
        w, h = _dims(spec["screen"], "screen")
        out["screen"] = screen_factory(min_width=w, max_width=w, min_height=h, max_height=h)
    if spec.get("window") is not None:
        out["window"] = _dims(spec["window"], "window")
    if spec.get("fonts") is not None:
        fonts = spec["fonts"]
        if not (isinstance(fonts, list) and len(fonts) <= 400
                and all(isinstance(f, str) and 0 < len(f) <= 100 for f in fonts)):
            raise ValueError("fingerprint.fonts must be a list of font family names")
        out["fonts"] = list(fonts)
    for flag in ("custom_fonts_only", "block_webgl", "fingerprint_preset"):
        if spec.get(flag) is not None:
            if not isinstance(spec[flag], bool):
                raise ValueError("fingerprint.%s must be a boolean" % flag)
            out[flag] = spec[flag]
    if spec.get("webgl_config") is not None:
        vendor, renderer = _pair(spec["webgl_config"], "webgl_config")
        if not (isinstance(vendor, str) and isinstance(renderer, str)):
            raise ValueError("fingerprint.webgl_config must be [vendor, renderer] strings")
        if "os" not in out:
            raise ValueError("fingerprint.webgl_config needs fingerprint.os")
        out["webgl_config"] = (vendor, renderer)
    if spec.get("humanize") is not None:
        humanize = spec["humanize"]
        if isinstance(humanize, bool):
            out["humanize"] = humanize
        elif isinstance(humanize, (int, float)) and 0.1 <= humanize <= 10:
            out["humanize"] = float(humanize)
        else:
            raise ValueError("fingerprint.humanize must be a boolean or 0.1..10 seconds")
    for raw in ("config", "firefox_user_prefs"):
        if spec.get(raw) is not None:
            out[raw] = _flat_dict(spec[raw], raw)
    if out.get("custom_fonts_only") and not out.get("fonts"):
        raise ValueError("fingerprint.custom_fonts_only needs fingerprint.fonts")
    return out


def default_spec():
    """The service-wide default spec (FORM_DEFAULT_FINGERPRINT, JSON) used when
    a request names none — e.g. the exit's language pinned instead of geoip's
    random CLDR draw (only 62% it-IT on an Italian exit). Invalid JSON or keys
    fail loudly at the first launch rather than silently dropping the spec."""
    import json
    import os
    raw = os.environ.get("FORM_DEFAULT_FINGERPRINT")
    if not raw:
        return None
    spec = json.loads(raw)
    launch_kwargs(spec, screen_factory=lambda **k: None)  # validate
    return spec


# FORM_FINGERPRINT_DIVERSIFY=1: each new form browser (and each pool profile,
# once) draws an OS and screen, so parallel sessions are not one device four
# times. Screens stay within the 1440x900 Xvfb display (a larger spoofed
# window would be clipped by the real one). Explicit spec values always win.
DIVERSE_OS = (("windows", 0.65), ("macos", 0.35))
DIVERSE_SCREENS = ((1366, 768), (1440, 900), (1280, 800), (1280, 720), (1360, 768))


def diversify(spec, rng=None):
    import os
    import random
    if os.environ.get("FORM_FINGERPRINT_DIVERSIFY") != "1":
        return spec
    rng = rng or random
    out = dict(spec or {})
    if "os" not in out:
        roll, acc = rng.random(), 0.0
        for name, weight in DIVERSE_OS:
            acc += weight
            if roll < acc:
                out["os"] = name
                break
        else:
            out["os"] = DIVERSE_OS[-1][0]
    if "screen" not in out:
        out["screen"] = list(rng.choice(DIVERSE_SCREENS))
    return out

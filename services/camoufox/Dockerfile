FROM python:3.13-slim

# Camoufox (the stealth Firefox build) needs the standard Playwright/
# Firefox system deps, plus xvfb: score-gated forms (reCAPTCHA v3) refuse the
# headless fingerprint, so /form-submit takes an opt-in `headed` browser that
# needs a display even in a container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg curl ca-certificates xvfb xauth \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgtk-3-0 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
    libgdk-pixbuf-2.0-0 libasound2 libxshmfence1 libx11-xcb1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Root Directory for this service is /services/camoufox (see README), so the
# build context IS this directory and COPY paths are relative to it. The
# scartoffie copy this came from used repo-root-relative paths because that
# project builds it with the repo root as context.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Download the Camoufox Firefox build + the GeoIP database into the image
# so the runtime container starts fast and offline.
ARG CAMOUFOX_BROWSER=official/stable/152.0.4-beta.30
# `sync` MUST precede `set`: without a refreshed manifest `set` cannot resolve
# the pin, exits 0 SILENTLY, and `fetch` then installs whatever the channel
# calls stable — that is how .31 (not this ARG) reached an image. The second
# `set` re-resolves the pin and is grepped for its confirmation line, and a
# disk walk checks the pinned bytes exist: drift fails the build here instead
# of shipping an unpinned browser.
RUN python -m camoufox sync && \
    python -m camoufox set "$CAMOUFOX_BROWSER" && \
    python -m camoufox fetch && \
    python -m camoufox set "$CAMOUFOX_BROWSER" 2>&1 | grep -F "Pinned: $CAMOUFOX_BROWSER" && \
    PIN="$CAMOUFOX_BROWSER" python -c 'import os,os.path as p,sys; pin=os.environ["PIN"].split("/")[-1]; sys.exit(0 if any(pin in n for b in ("~/.cache/camoufox","~/.local/share/camoufox") for r,d,f in os.walk(p.expanduser(b)) for n in d+f) else "pinned "+pin+" not on disk")'

COPY app.py form_flow.py form_worker.py captcha_solver.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# ONE warmed Camoufox page per container (WORKERS=1) — the persistent SPA
# session + its sensor cookie can't be shared across processes, and a
# second Camoufox in one container thrashes Railway CPU. Scale by replicas
# if ever needed (each warms its own session).
# No PORT default on purpose — see the note in services/scrapling/Dockerfile. The
# Railway variable is the single source of truth for both the bind and the URL
# that reaches it, so a missing one must fail loudly rather than diverge.
# Firefox's content sandbox needs user namespaces, which the container host
# sometimes takes away — measured twice on 2026-09-26: every launch in every
# mode died with `CanCreateUserNamespace() clone() failure: EACCES` + SIGSEGV
# until a restart landed the container on a host that still has them. A
# restart is not a fix for a fleet, so the content sandbox stays off here:
# sites cannot see it (it is OS isolation, not a fingerprint surface), while
# a fleet that cannot launch is down for every consumer at once.
ENV MOZ_DISABLE_CONTENT_SANDBOX=1
ENV WORKERS=1
EXPOSE 8000
# Bind 0.0.0.0 (Railway's edge talks IPv4; an IPv6-only bind 502s).
# The entrypoint starts Xvfb (for opt-in headed browsers) and execs uvicorn;
# see it for why this is a script and not an xvfb-run one-liner.
CMD ["./entrypoint.sh"]

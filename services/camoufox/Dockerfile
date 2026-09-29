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
# Order is the fix: FETCH THE PIN EXPLICITLY first, then pin, then assert.
# `fetch` starts by wiping a non-empty INSTALL_DIR when its compat flag is
# absent — on a fresh image that deletes config.json, so a pin written before
# a plain `fetch` is destroyed and the channel's latest ships instead
# (observed 2026-09-27: set pinned beta.30, fetch installed beta.31).
# `set` after fetch pins runtime launches; `active` must then name the pin
# and never "(not fetched)" or the build fails instead of shipping drift.
RUN python -m camoufox fetch "$CAMOUFOX_BROWSER" && \
    python -m camoufox set "$CAMOUFOX_BROWSER" 2>&1 | grep -F "Pinned: $CAMOUFOX_BROWSER" && \
    python -m camoufox active | tee /tmp/active && \
    grep -F "${CAMOUFOX_BROWSER##*/}" /tmp/active && \
    ! grep -F "not fetched" /tmp/active

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
# A form wedged past deadline + grace leaks its browser forever (the stuck
# thread owns the handle); restart the container 3s after the 502 flushes so
# launches never decay. 0 disables. See form_worker._shed_delay_s.
ENV FORM_WEDGE_EXIT_S=3
ENV WORKERS=1
EXPOSE 8000
# Bind 0.0.0.0 (Railway's edge talks IPv4; an IPv6-only bind 502s).
# The entrypoint starts Xvfb (for opt-in headed browsers) and execs uvicorn;
# see it for why this is a script and not an xvfb-run one-liner.
CMD ["./entrypoint.sh"]

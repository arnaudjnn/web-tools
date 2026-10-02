FROM python:3.13-slim

# Camoufox + Patchright (the stealth Firefox build Scrapling uses) needs
# the standard Playwright system deps. Install once at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg curl ca-certificates \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgtk-3-0 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
    libgdk-pixbuf-2.0-0 libasound2 libxshmfence1 libx11-xcb1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Downloads the browser binaries into the image so the runtime container starts
# fast and offline.
#
# BOTH commands are required, and `scrapling install` alone is not enough:
# StealthySession on 0.4.x drives Patchright's Chromium, which `scrapling
# install` does not fetch. Without the second line the container starts clean and
# then fails every fetch at runtime with
#   BrowserType.launch_persistent_context: Executable doesn't exist at
#   /root/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome
# which is a 502 per request, not a failed build. Re-check this if the pinned
# Scrapling version changes engines again.
RUN scrapling install \
 && patchright install chromium \
 && patchright install-deps chromium

# web_agent (POST /agent): browser-use in its OWN venv, because it cannot be
# resolved next to scrapling[fetchers]==0.4.14 (hard anyio + markdownify pins;
# evidence in requirements-agent.txt). Same image, no new service. patchright is
# installed at exactly the main venv's version so both share the Chromium
# downloaded above — no second browser in the image; the build fails if they
# drift. After the browser layer so editing this does not re-download Chromium.
COPY requirements-agent.txt .
RUN python -m venv /opt/agent-venv \
 && PR="$(python -c 'import importlib.metadata as m; print(m.version("patchright"))')" \
 && /opt/agent-venv/bin/pip install --no-cache-dir -r requirements-agent.txt "patchright==${PR}" \
 && test "$(/opt/agent-venv/bin/python -c 'import importlib.metadata as m; print(m.version("patchright"))')" = "${PR}" \
 && /opt/agent-venv/bin/python -c "import browser_use, patchright"
ENV AGENT_PYTHON=/opt/agent-venv/bin/python \
    AGENT_PROFILE_DIR=/tmp/web-agent-profile

COPY app.py agent.py agent_runner.py ./

# WORKERS=1. Each worker lazily builds up to one browser PER MODE (fast /
# stealth / solve), so the worst case is WORKERS * 3 live Chromium instances.
# A web_agent run adds ONE more, short-lived (a per-container flock allows one
# run at a time, whatever WORKERS is), so the ceiling at WORKERS=1 is four.
#
# 2 was not survivable. Six browsers exhausted the container's THREAD budget, not
# its memory — Chromium died on `pthread_create: Resource temporarily unavailable
# (11)` and every fetch afterwards returned 502 or timed out, so LinkedIn silently
# fell back to the datacenter IP. The earlier sizing note reasoned about ~300MB
# per browser and missed that the binding limit here is threads/PIDs.
#
# The parallelism lost is smaller than it looks: each mode already serialises on
# its own single-slot executor, so a second worker only helped when two requests
# of the SAME mode overlapped. Scale with replicas instead — a replica is a fresh
# container with its own budget, which is what the Camoufox service does.
# No PORT default on purpose. The bind and the URL that reaches it have to come
# from ONE source of truth, and that source is the Railway variable.
#
# With `ENV PORT=8000` here, the two sides fell back differently: the container
# still bound 8000 from this default while `${{Scrapling.PORT}}` — which can only
# see a Railway variable — resolved to an empty string, so a consumer silently got
# `http://scrapling.railway.internal:` and every fetch failed far from the cause.
# Without a default, a missing variable stops the container instead.
ENV WORKERS=1
EXPOSE 8000
# Bind to 0.0.0.0. Railway's edge router talks IPv4 to the container
# (so an IPv6-only bind returns 502), and Railway's private DNS for
# *.railway.internal returns both A and AAAA records so sibling services
# can still reach us over the private network.
#
# --workers ${WORKERS} runs N independent Python processes, each owning
# one StealthySession lazily on first request. Patchright's sync API
# can't coexist with FastAPI's asyncio loop in the same process, so
# parallelism is achieved across processes rather than within.
CMD ["sh", "-c", ": \"${PORT:?PORT must be set — this image has no default, so a missing Railway variable fails here instead of silently handing callers a portless URL}\" && exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]

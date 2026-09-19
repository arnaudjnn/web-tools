FROM python:3.13-slim

# Camoufox (the stealth Firefox build) needs the standard Playwright/
# Firefox system deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg curl ca-certificates \
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
RUN python -m camoufox fetch

COPY app.py form_flow.py .

# ONE warmed Camoufox page per container (WORKERS=1) — the persistent SPA
# session + its sensor cookie can't be shared across processes, and a
# second Camoufox in one container thrashes Railway CPU. Scale by replicas
# if ever needed (each warms its own session).
# No PORT default on purpose — see the note in services/scrapling/Dockerfile. The
# Railway variable is the single source of truth for both the bind and the URL
# that reaches it, so a missing one must fail loudly rather than diverge.
ENV WORKERS=1
EXPOSE 8000
# Bind 0.0.0.0 (Railway's edge talks IPv4; an IPv6-only bind 502s).
CMD ["sh", "-c", ": \"${PORT:?PORT must be set — this image has no default, so a missing Railway variable fails here instead of silently handing callers a portless URL}\" && exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]

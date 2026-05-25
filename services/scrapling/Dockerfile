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
# Downloads ~200 MB of Camoufox + Patchright binaries into the image so
# the runtime container starts fast and offline.
RUN scrapling install

COPY app.py .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]

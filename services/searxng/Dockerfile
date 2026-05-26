FROM searxng/searxng:latest

COPY settings.yml /etc/searxng/settings.yml.tpl
COPY entrypoint.py /usr/local/bin/searxng-entrypoint.py
RUN chmod +x /usr/local/bin/searxng-entrypoint.py

ADD google_sorry_fix.py /tmp/google_sorry_fix.py
RUN GOOGLE_PY=/usr/local/searxng/searx/engines/google.py && \
    python3 /tmp/google_sorry_fix.py "$GOOGLE_PY" && \
    rm /tmp/google_sorry_fix.py && \
    find /usr/local/searxng/searx -name "__pycache__" -type d -exec rm -rf {} + && \
    python3 -m compileall -q -f -j 0 --invalidation-mode=unchecked-hash /usr/local/searxng/searx/

# entrypoint.py reads WEBSHARE_API_KEY at startup, pulls the full
# residential pool, writes the proxy list into settings.yml under
# outgoing.proxies.all://, then execs granian. SearXNG then
# round-robins requests across the pool natively, retrying on a
# different proxy when an engine returns CAPTCHA / rate-limit.
ENTRYPOINT ["python3", "/usr/local/bin/searxng-entrypoint.py"]

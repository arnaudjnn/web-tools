# Pinned, not floating (was :latest). google_sorry_fix.py patches this image's
# google.py and exits 1 when a patch stops matching, so a bump is a deliberate
# build that either passes the patcher or fails loudly. 2026.9.30-a9d990033 was
# :latest on 2026-10-02 (digest sha256:a07a5cd2…); patch 1 is upstream there.
FROM searxng/searxng:2026.9.30-a9d990033

COPY settings.yml /etc/searxng/settings.yml.tpl

ADD google_sorry_fix.py /tmp/google_sorry_fix.py
RUN GOOGLE_PY=/usr/local/searxng/searx/engines/google.py && \
    python3 /tmp/google_sorry_fix.py "$GOOGLE_PY" && \
    rm /tmp/google_sorry_fix.py && \
    find /usr/local/searxng/searx -name "__pycache__" -type d -exec rm -rf {} + && \
    python3 -m compileall -q -f -j 0 --invalidation-mode=unchecked-hash /usr/local/searxng/searx/

ENTRYPOINT ["sh", "-c", "\
if [ -n \"$PROXY_URL\" ]; then \
  sed \"s|\\${PROXY_URL}|$PROXY_URL|g\" /etc/searxng/settings.yml.tpl > /etc/searxng/settings.yml; \
  echo Proxy: enabled; \
else \
  sed '/proxies:/,/PROXY_URL/d' /etc/searxng/settings.yml.tpl > /etc/searxng/settings.yml; \
  echo Proxy: disabled; \
fi && \
exec /usr/local/searxng/.venv/bin/granian --interface wsgi --host 0.0.0.0 --port 8080 --workers 4 --blocking-threads 8 searx.webapp:app"]

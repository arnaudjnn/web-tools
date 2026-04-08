FROM searxng/searxng:latest

COPY settings.yml /etc/searxng/settings.yml.tpl

ENTRYPOINT ["sh", "-c", "\
if [ -n \"$PROXY_URL\" ]; then \
  sed \"s|\\${PROXY_URL}|$PROXY_URL|g\" /etc/searxng/settings.yml.tpl > /etc/searxng/settings.yml; \
  echo Proxy: enabled; \
else \
  sed '/proxies:/,/PROXY_URL/d' /etc/searxng/settings.yml.tpl > /etc/searxng/settings.yml; \
  echo Proxy: disabled; \
fi && \
exec /usr/local/searxng/.venv/bin/granian --interface wsgi --host 0.0.0.0 --port 8080 --workers 4 --blocking-threads 8 searx.webapp:app"]

FROM debian:bookworm-slim AS download
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
WORKDIR /download
# Pin both the engine release and archive digest; never execute a latest URL.
RUN case "$TARGETARCH" in \
      amd64) arch=x86_64; digest=9e5d9d081909ea983bc8c94999bb3d411fd6b74a9788504295b7e25f84310505 ;; \
      arm64) arch=aarch64; digest=fd422a9bc0cb38047d270c2d0ee393df5c4d54fa9d0130d39917e858cd7020ae ;; \
      *) exit 1 ;; \
    esac \
    && curl --fail --location --retry 2 --max-time 120 -o obscura.tar.gz \
      "https://github.com/h4ckf0r0day/obscura/releases/download/v0.2.2/obscura-${arch}-linux.tar.gz" \
    && echo "$digest  obscura.tar.gz" | sha256sum -c - \
    && tar -xzf obscura.tar.gz obscura

FROM node:22-bookworm-slim
COPY --from=download /download/obscura /usr/local/bin/obscura
WORKDIR /app
COPY --chown=node:node cdp.mjs server.mjs ./
USER node
EXPOSE 8000
CMD ["node", "server.mjs"]

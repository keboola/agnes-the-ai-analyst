FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ARG AGNES_VERSION=dev
ARG RELEASE_CHANNEL=dev
ARG AGNES_COMMIT_SHA=unknown
ARG AGNES_TAG=unknown
ENV AGNES_VERSION=${AGNES_VERSION}
ENV RELEASE_CHANNEL=${RELEASE_CHANNEL}
ENV AGNES_COMMIT_SHA=${AGNES_COMMIT_SHA}
ENV AGNES_TAG=${AGNES_TAG}

WORKDIR /app

COPY . .

# Bake every host-side artifact at /opt/agnes-host/ — the contract path
# VM startup uses to extract files via `docker create` + `docker cp`
# instead of curling from raw.githubusercontent.com/main. Pins host
# artifacts to AGNES_TAG the same way the app is already pinned —
# eliminates the split-brain where the immutable image runs against
# arbitrary main-branch compose files / bash scripts.
#
# Includes:
#   - agnes-auto-upgrade.sh — host cron driver (5-min digest poll)
#   - agnes-tls-rotate.sh — host cron driver (daily corp-PKI cert refetch)
#   - tls-fetch.sh — generic URL fetcher (sm:// gs:// https:// file://)
#   - docker-compose.{yml,prod.yml,host-mount.yml,tls.yml} — host runtime
#   - Caddyfile — TLS reverse proxy config
#
# Why a copy out of /app instead of pointing at /app directly:
#   /app is owned by uid 999 (USER agnes below); /opt/agnes-host is
#   root-owned, mode 0755 across the board, stable path that won't
#   shift if /app structure refactors. Stable contract for `docker cp`
#   consumers.
RUN mkdir -p /opt/agnes-host && \
    cp /app/scripts/ops/agnes-auto-upgrade.sh \
       /app/scripts/ops/agnes-tls-rotate.sh \
       /app/scripts/tls-fetch.sh \
       /opt/agnes-host/ && \
    cp /app/docker-compose.yml /app/docker-compose.prod.yml \
       /app/docker-compose.host-mount.yml /app/docker-compose.tls.yml \
       /app/Caddyfile /opt/agnes-host/ && \
    chmod 0755 /opt/agnes-host/agnes-auto-upgrade.sh \
              /opt/agnes-host/agnes-tls-rotate.sh \
              /opt/agnes-host/tls-fetch.sh && \
    chmod 0644 /opt/agnes-host/docker-compose.yml \
              /opt/agnes-host/docker-compose.prod.yml \
              /opt/agnes-host/docker-compose.host-mount.yml \
              /opt/agnes-host/docker-compose.tls.yml \
              /opt/agnes-host/Caddyfile

# Build wheel artifact (served at /cli/download)
RUN uv build --wheel --out-dir /app/dist

# Install production dependencies from pyproject.toml
RUN uv pip install --system --no-cache .

# Run as non-root user for container hardening (C13).
# uid/gid pinned to 999 so host-side chown in startup-script.sh.tpl can match
# without parsing /etc/passwd inside the image. Changing this number breaks
# every existing PD-backed deploy until the operator re-chowns /data.
RUN useradd --system --uid 999 --create-home --shell /usr/sbin/nologin agnes && \
    mkdir -p /data && chown -R agnes:agnes /data && \
    chown -R agnes:agnes /app
USER agnes

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

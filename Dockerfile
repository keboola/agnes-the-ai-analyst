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

# Memory-allocator tuning. BigQuery/DuckDB query churn produces large transient
# native allocations; glibc's default per-CPU arenas (≈8×cores) retain freed
# memory and never return it to the OS, and on a host with Transparent Huge
# Pages = always each retained region is backed by a 2 MiB huge page — so RSS
# ratchets up to the largest concurrent working set and stays there, eventually
# tripping the cgroup OOM killer on data-source-heavy instances. Capping arenas
# to 2 and lowering the trim threshold (128 KiB) forces glibc to release freed
# memory back to the kernel. Negligible CPU cost for this I/O-bound workload
# (DuckDB manages its own buffer pool). Host-side THP=madvise is the companion
# mitigation, applied in infra/modules/customer-instance/startup-script.sh.tpl.
ENV MALLOC_ARENA_MAX=2
ENV MALLOC_TRIM_THRESHOLD_=131072

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
#   - agnes-state-applier.{sh,service,timer} — DB backend state machine
#     (applies compose lifecycle changes when /data/state/db-state-target.flag changes)
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
       /app/scripts/ops/agnes-state-applier.sh \
       /app/scripts/ops/agnes-state-applier.service \
       /app/scripts/ops/agnes-state-applier.timer \
       /app/scripts/ops/agnes-state-applier-bootstrap.service \
       /app/scripts/tls-fetch.sh \
       /opt/agnes-host/ && \
    cp /app/docker-compose.yml /app/docker-compose.prod.yml \
       /app/docker-compose.host-mount.yml /app/docker-compose.tls.yml \
       /app/docker-compose.postgres.yml \
       /app/docker-compose.postgres-host-mount.yml \
       /app/docker-compose.gcp-logging.yml \
       /app/Caddyfile /opt/agnes-host/ && \
    chmod 0755 /opt/agnes-host/agnes-auto-upgrade.sh \
              /opt/agnes-host/agnes-tls-rotate.sh \
              /opt/agnes-host/agnes-state-applier.sh \
              /opt/agnes-host/tls-fetch.sh && \
    chmod 0644 /opt/agnes-host/agnes-state-applier.service \
              /opt/agnes-host/agnes-state-applier.timer \
              /opt/agnes-host/agnes-state-applier-bootstrap.service \
              /opt/agnes-host/docker-compose.yml \
              /opt/agnes-host/docker-compose.prod.yml \
              /opt/agnes-host/docker-compose.host-mount.yml \
              /opt/agnes-host/docker-compose.tls.yml \
              /opt/agnes-host/docker-compose.postgres.yml \
              /opt/agnes-host/docker-compose.postgres-host-mount.yml \
              /opt/agnes-host/docker-compose.gcp-logging.yml \
              /opt/agnes-host/Caddyfile

# Build wheel artifact (served at /cli/download)
RUN uv build --wheel --out-dir /app/dist

# Install production dependencies from pyproject.toml. The `[server]` extra
# pulls in connectors-only deps (kbcstorage) that the CLI wheel deliberately
# omits; `[slack-socket]` adds slack_sdk so the optional Slack Socket Mode
# inbound transport works out-of-the-box in the server image (HTTP-only
# deployments simply never enable it; the import stays lazy + fail-closed).
# See [project.optional-dependencies] in pyproject.toml.
RUN uv pip install --system --no-cache ".[server,slack-socket]"

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

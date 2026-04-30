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

# Bake host-side scripts at a stable, well-known path so VM startup can
# extract them via `docker create` + `docker cp` instead of curling from
# raw.githubusercontent.com/main. This pins host scripts to AGNES_TAG
# the same way it already pins the app — eliminates the split-brain
# where the immutable image runs against an arbitrary main-branch
# bash script.
#
# Why a copy instead of pointing at /app/scripts/ops directly:
#   /app is owned by uid 999 and the path may shift; /opt/agnes-host-scripts
#   is the contract for `docker cp` consumers. Stable path, root-readable,
#   permissions guaranteed.
RUN mkdir -p /opt/agnes-host-scripts && \
    cp /app/scripts/ops/agnes-auto-upgrade.sh /opt/agnes-host-scripts/agnes-auto-upgrade.sh && \
    chmod 0755 /opt/agnes-host-scripts/agnes-auto-upgrade.sh

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

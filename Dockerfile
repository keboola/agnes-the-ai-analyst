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

# Build wheel artifact (served at /cli/download)
RUN uv build --wheel --out-dir /app/dist

# Install production dependencies from pyproject.toml
RUN uv pip install --system --no-cache .

# Run as non-root user for container hardening (C13)
RUN useradd --system --create-home --shell /usr/sbin/nologin agnes && \
    mkdir -p /data && chown -R agnes:agnes /data && \
    chown -R agnes:agnes /app
USER agnes

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

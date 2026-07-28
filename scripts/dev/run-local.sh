#!/usr/bin/env bash
# Run Agnes uvicorn locally with OAuth + CLI wheel wired up.
#
# Default mode pulls Google OAuth client id / secret from GCP Secret Manager
# (project given by AGNES_OAUTH_GCP_PROJECT env var; secrets named
# `agnes-google-client-id` and `agnes-google-client-secret`) and points the
# CLI wheel endpoint at the locally-built ./dist so /cli/wheel/<name>.whl
# resolves. Pass --dev to skip OAuth entirely and run with LOCAL_DEV_MODE=1.
#
# Intentionally does NOT source .env — that file carries DEBUG=1 +
# AGNES_DEBUG_AUTH=1, which switches on fastapi-debug-toolbar and the
# /auth/debug surface. Set those manually if you want them. (LOCAL_DEV_MODE=1
# also enables the toolbar — see app/main.py.)
#
# Usage:
#   AGNES_OAUTH_GCP_PROJECT=<project> ./scripts/dev/run-local.sh   # OAuth login
#   ./scripts/dev/run-local.sh --dev                                # auto-login as dev@localhost
#   PORT=8000 ./scripts/dev/run-local.sh --dev                      # override port (default 8765)
#   HOST=0.0.0.0 ./scripts/dev/run-local.sh --dev                   # bind all interfaces
#   LOCAL_DEV_USER_EMAIL=foo@bar ./scripts/dev/run-local.sh --dev   # custom dev user email
#   AGNES_HOME_ROUTE=/dashboard ./scripts/dev/run-local.sh --dev    # land on /dashboard not /home
set -euo pipefail

cd "$(dirname "$0")/../.."

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"
# Operator's GCP project hosting `agnes-google-client-id` / `agnes-google-client-secret`
# secrets (only used when --dev is NOT passed). Set via env to avoid hardcoding
# operator-specific identifiers in OSS source.
GCP_PROJECT="${AGNES_OAUTH_GCP_PROJECT:-}"
DEV_MODE=0

for arg in "$@"; do
    case "$arg" in
        --dev) DEV_MODE=1 ;;
        *) echo "warning: unknown arg $arg (passing through to uvicorn)" >&2 ;;
    esac
done

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: gcloud CLI not found in PATH" >&2
    exit 1
fi

if [ ! -x .venv/bin/uvicorn ]; then
    echo "error: .venv/bin/uvicorn missing — run: uv sync" >&2
    exit 1
fi

if [ "$DEV_MODE" -eq 1 ]; then
    echo "LOCAL_DEV_MODE=1 — auth bypassed, auto-login as ${LOCAL_DEV_USER_EMAIL:-dev@localhost}"
    export LOCAL_DEV_MODE=1
    # Default mock groups so /profile + group-aware code render correctly.
    # Override or disable via env: LOCAL_DEV_GROUPS='[...]' or LOCAL_DEV_GROUPS=
    if [ -z "${LOCAL_DEV_GROUPS+x}" ]; then
        export LOCAL_DEV_GROUPS='[{"id":"local-dev-engineers@example.com","name":"Local Dev Engineers"},{"id":"local-dev-admins@example.com","name":"Local Dev Admins"}]'
    fi
    # Land on the new state-aware /home page rather than the legacy /dashboard.
    # Customer-fork-style default for dev iteration; OSS default stays /dashboard.
    # Override via env: AGNES_HOME_ROUTE=/dashboard ./scripts/dev/run-local.sh --dev
    if [ -z "${AGNES_HOME_ROUTE+x}" ]; then
        export AGNES_HOME_ROUTE=/home
    fi
else
    if [ -z "$GCP_PROJECT" ]; then
        echo "error: AGNES_OAUTH_GCP_PROJECT not set." >&2
        echo "  Either pass --dev (no OAuth, auto-login as dev@localhost), or:" >&2
        echo "  export AGNES_OAUTH_GCP_PROJECT=<your-gcp-project>" >&2
        echo "  (the project hosting agnes-google-client-id / agnes-google-client-secret)" >&2
        exit 1
    fi
    echo "Fetching OAuth secrets from GCP project ${GCP_PROJECT}..."
    GOOGLE_CLIENT_ID="$(gcloud secrets versions access latest \
        --secret=agnes-google-client-id --project="${GCP_PROJECT}")"
    GOOGLE_CLIENT_SECRET="$(gcloud secrets versions access latest \
        --secret=agnes-google-client-secret --project="${GCP_PROJECT}")"
    export GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET
fi

# Build wheel if missing — /cli/latest needs ./dist/*.whl on disk.
if ! ls dist/*.whl >/dev/null 2>&1; then
    echo "No wheel in ./dist — building..."
    uv build --wheel
fi
export AGNES_CLI_DIST_DIR="${PWD}/dist"

echo "Starting uvicorn on http://${HOST}:${PORT}"
exec .venv/bin/uvicorn app.main:app --reload --host "${HOST}" --port "${PORT}"

#!/bin/bash
# Fetch a TLS artifact (cert chain or private key) from a URL to a local
# path with the requested file mode. Supported URL schemes:
#
#   sm://<secret-name>       — Google Secret Manager, latest version
#   gs://<bucket>/<path>     — GCS object
#   https://<url>            — plain HTTPS download
#   file://<path>            — local file copy (dev/testing only)
#
# Usage: tls-fetch.sh <url> <dest> [mode]
#
# Writes atomically via a temp file + install(1) so Caddy never sees a
# half-written cert. Exits non-zero on any failure — callers should not
# swallow errors (a silent TLS break is worse than a loud one).
set -euo pipefail

URL="${1:?usage: tls-fetch.sh <url> <dest> [mode]}"
DEST="${2:?usage: tls-fetch.sh <url> <dest> [mode]}"
MODE="${3:-644}"

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

case "$URL" in
  sm://*)
    SECRET="${URL#sm://}"
    gcloud secrets versions access latest --secret="$SECRET" > "$TMP"
    ;;
  gs://*)
    gsutil -q cp "$URL" "$TMP"
    ;;
  https://*)
    curl -fsSL "$URL" -o "$TMP"
    ;;
  file://*)
    cp "${URL#file://}" "$TMP"
    ;;
  *)
    echo "tls-fetch: unsupported URL scheme: $URL" >&2
    exit 2
    ;;
esac

if [ ! -s "$TMP" ]; then
  echo "tls-fetch: fetched empty file from $URL" >&2
  exit 3
fi

install -m "$MODE" "$TMP" "$DEST"

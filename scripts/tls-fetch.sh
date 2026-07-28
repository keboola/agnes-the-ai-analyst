#!/bin/bash
# Fetch a TLS artifact (cert chain or private key) from a URL to a local
# path with the requested file mode. Supported URL schemes:
#
#   sm://<secret-name>       — Google Secret Manager, latest version
#   gs://<bucket>/<path>     — GCS object
#   https://<url>            — plain HTTPS download (no redirects, no
#                              scheme downgrade — see curl flags below)
#   file://<path>            — local file copy (dev/testing only)
#
# Usage: tls-fetch.sh <url> <dest> [mode] [kind]
#
#   kind: cert (default) | key — controls post-fetch PEM validation.
#         "cert" runs `openssl x509 -noout`, "key" runs `openssl pkey
#         -noout`. Anything garbage (HTML error page from a corp portal,
#         truncated body, unrelated file) is rejected loudly here so
#         Caddy never tries to load an unparseable cert.
#
# Writes atomically via a temp file + install(1) so Caddy never sees a
# half-written cert. Exits non-zero on any failure — callers should not
# swallow errors (a silent TLS break is worse than a loud one).
#
# Exit codes:
#   2 — unsupported URL scheme
#   3 — fetched file is empty
#   4 — fetched content is not a valid PEM of the requested kind
set -euo pipefail

URL="${1:?usage: tls-fetch.sh <url> <dest> [mode] [kind]}"
DEST="${2:?usage: tls-fetch.sh <url> <dest> [mode] [kind]}"
MODE="${3:-644}"
KIND="${4:-cert}"

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
    # --max-redirs 0: a redirect on a TLS-artifact URL is a smell
    # (compromised DNS / hijacked endpoint can swap the cert/key for
    # an attacker-controlled one). Fail loud instead.
    # --proto '=https': refuse if curl would degrade scheme.
    # --retry 2: tolerate single transient blips; daily timer means
    # extended outages are caught the next tick anyway.
    curl -fsS --max-redirs 0 --proto '=https' --retry 2 "$URL" -o "$TMP"
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

# PEM sanity check. Catches: HTML error pages with 200 OK, truncated
# downloads, and anything that's not a parseable PEM of the requested
# kind. Cheaper to fail here than to let Caddy crash on reload.
case "$KIND" in
  cert)
    if ! openssl x509 -in "$TMP" -noout 2>/dev/null; then
      echo "tls-fetch: $URL did not return a valid PEM certificate" >&2
      exit 4
    fi
    ;;
  key)
    if ! openssl pkey -in "$TMP" -noout 2>/dev/null; then
      echo "tls-fetch: $URL did not return a valid PEM private key" >&2
      exit 4
    fi
    ;;
  *)
    echo "tls-fetch: unsupported kind: $KIND (expected cert|key)" >&2
    exit 2
    ;;
esac

install -m "$MODE" "$TMP" "$DEST"

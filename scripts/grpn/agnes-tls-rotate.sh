#!/bin/bash
# Deployed to /usr/local/bin/agnes-tls-rotate.sh on the VM by the infra
# repo startup.sh. A systemd timer fires it daily.
#
# Corp security rotates certs at stable URLs (TLS_FULLCHAIN_URL,
# TLS_PRIVKEY_URL in /opt/agnes/.env). This script refetches, compares
# sha via cmp, atomically replaces changed files, and sends SIGUSR1 to
# caddy for a zero-downtime reload. No-op when cert has not moved.
#
# TLS_PRIVKEY_URL is optional — leave empty when the key is provisioned
# once per VM (e.g. from Secret Manager at boot) and reused across
# cert rotations.
#
# Self-signed fallback: when TLS_FULLCHAIN_URL returns no data (security
# dept hasn't published the real cert yet) AND no fullchain.pem exists
# on disk, generate a 30-day self-signed cert against the same privkey.
# Because Security signs the eventual real cert against the CSR
# produced from this same key, the key never changes — the rotate tick
# after publication just swaps the fullchain file, SIGUSR1-reloads
# Caddy, and clients start seeing the real chain with zero downtime.
# Browsers see a self-signed warning in the meantime — acceptable for
# the bring-up window, and the only way to get Caddy up before the
# real cert exists without splitting into two code paths.
set -euo pipefail

cd /opt/agnes
# shellcheck disable=SC1091
set -a; . /opt/agnes/.env; set +a

[ -n "${TLS_FULLCHAIN_URL:-}" ] || { echo "TLS_FULLCHAIN_URL empty — nothing to rotate"; exit 0; }

CERT_DIR=/data/state/certs
mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"

CHANGED=0
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT

refetch() {
  local url="$1" dest="$2" mode="$3"
  # IMPORTANT: tls-fetch.sh may fail (404, empty body, auth error). When
  # the caller sits behind `if ! refetch`, bash disables `set -e` for
  # everything inside the condition — so without an explicit exit-code
  # check we would fall through to `install` and overwrite $dest with
  # whatever stale bytes the PREVIOUS refetch call left in $TMP. That
  # turned the "fullchain unavailable → fall back to self-signed" branch
  # into a "fullchain file filled with privkey bytes" bug. Check
  # explicitly and return 1 on any fetch failure so the caller's
  # fallback branch fires cleanly.
  if ! /usr/local/bin/tls-fetch.sh "$url" "$TMP" "$mode"; then
    return 1
  fi
  if [ ! -f "$dest" ] || ! cmp -s "$TMP" "$dest"; then
    install -m "$mode" "$TMP" "$dest"
    echo "$(date -Is) rotated $(basename "$dest")"
    CHANGED=1
  fi
}

# Private key first — needed by self-signed fallback if fullchain is
# unavailable. When TLS_PRIVKEY_URL is empty, operators must pre-seed
# /data/state/certs/privkey.pem out-of-band (e.g. from a snapshot).
if [ -n "${TLS_PRIVKEY_URL:-}" ]; then
  if ! refetch "$TLS_PRIVKEY_URL" "$CERT_DIR/privkey.pem" 600; then
    if [ ! -s "$CERT_DIR/privkey.pem" ]; then
      echo "ERROR: privkey fetch failed and no cached copy exists — aborting" >&2
      exit 1
    fi
    echo "$(date -Is) privkey fetch failed; keeping cached $CERT_DIR/privkey.pem"
  fi
fi

# Real cert fetch. On failure, fall back to self-signed IFF no
# fullchain exists yet. If one exists (prior real OR prior self-signed)
# keep it — a transient fetch failure should not churn certs.
if ! refetch "$TLS_FULLCHAIN_URL" "$CERT_DIR/fullchain.pem" 644; then
  if [ ! -s "$CERT_DIR/fullchain.pem" ]; then
    echo "$(date -Is) real cert unavailable at $TLS_FULLCHAIN_URL — generating 30-day self-signed"
    if [ ! -s "$CERT_DIR/privkey.pem" ]; then
      echo "ERROR: no privkey available — cannot self-sign" >&2
      exit 1
    fi
    CN="${DOMAIN:-localhost}"
    openssl req -x509 -new -key "$CERT_DIR/privkey.pem" \
      -out "$CERT_DIR/fullchain.pem" -days 30 \
      -subj "/C=US/ST=Illinois/L=Chicago/O=Groupon, Inc./CN=$CN" \
      -addext "subjectAltName=DNS:$CN" \
      -addext "keyUsage=digitalSignature,keyEncipherment" \
      -addext "extendedKeyUsage=serverAuth" 2>/dev/null
    chmod 644 "$CERT_DIR/fullchain.pem"
    echo "$(date -Is) self-signed fullchain.pem installed (CN=$CN)"
    CHANGED=1
  else
    echo "$(date -Is) fetch failed but cached fullchain.pem exists — keeping it"
  fi
fi

if [ "$CHANGED" -eq 1 ]; then
  COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml -f docker-compose.tls.yml"
  if docker compose $COMPOSE_FILES --profile tls ps --status=running --format '{{.Service}}' 2>/dev/null | grep -q '^caddy$'; then
    # Caddy running — graceful reload via SIGUSR1 picks up the new
    # cert without dropping connections.
    docker compose $COMPOSE_FILES --profile tls kill -s SIGUSR1 caddy >/dev/null 2>&1 \
      && echo "$(date -Is) caddy reloaded" \
      || echo "$(date -Is) caddy reload signal failed"
  else
    # Caddy not running yet — first time certs land on this VM, or
    # operator hasn't brought up the tls profile yet. Flip the stack
    # in place so this script is self-sufficient: no separate manual
    # `docker compose up` step after seeding certs.
    echo "$(date -Is) caddy not running — bringing tls profile up"
    docker compose $COMPOSE_FILES --profile tls up -d 2>&1 | tail -5
  fi
fi

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
  /usr/local/bin/tls-fetch.sh "$url" "$TMP" "$mode"
  if [ ! -f "$dest" ] || ! cmp -s "$TMP" "$dest"; then
    install -m "$mode" "$TMP" "$dest"
    echo "$(date -Is) rotated $(basename "$dest")"
    CHANGED=1
  fi
}

refetch "$TLS_FULLCHAIN_URL" "$CERT_DIR/fullchain.pem" 644
if [ -n "${TLS_PRIVKEY_URL:-}" ]; then
  refetch "$TLS_PRIVKEY_URL" "$CERT_DIR/privkey.pem" 600
fi

if [ "$CHANGED" -eq 1 ]; then
  COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml -f docker-compose.tls.yml"
  # SIGUSR1 = graceful Caddy config reload, no connection drop.
  if docker compose $COMPOSE_FILES --profile tls kill -s SIGUSR1 caddy 2>/dev/null; then
    echo "$(date -Is) caddy reloaded"
  else
    # Caddy not running yet (first boot before initial compose up). Safe to skip —
    # startup flow will bring it up with the new files.
    echo "$(date -Is) caddy not running — skipping reload"
  fi
fi

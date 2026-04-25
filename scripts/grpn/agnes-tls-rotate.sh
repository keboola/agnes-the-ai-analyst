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
  local url="$1" dest="$2" mode="$3" kind="$4"
  # IMPORTANT: tls-fetch.sh may fail (404, empty body, auth error,
  # invalid PEM, redirect attempt). When the caller sits behind
  # `if ! refetch`, bash disables `set -e` for everything inside the
  # condition — so without an explicit exit-code check we would fall
  # through to `install` and overwrite $dest with whatever stale bytes
  # the PREVIOUS refetch call left in $TMP. That turned the "fullchain
  # unavailable → fall back to self-signed" branch into a "fullchain
  # file filled with privkey bytes" bug. Check explicitly and return 1
  # on any fetch failure so the caller's fallback branch fires cleanly.
  if ! /usr/local/bin/tls-fetch.sh "$url" "$TMP" "$mode" "$kind"; then
    return 1
  fi
  if [ ! -f "$dest" ] || ! cmp -s "$TMP" "$dest"; then
    install -m "$mode" "$TMP" "$dest"
    echo "$(date -Is) rotated $(basename "$dest")"
    CHANGED=1
  fi
}

# Private key handling.
#
# Three modes (decided per-VM in the infra repo's local.vm_tls):
#
#   1. TLS_PRIVKEY_URL set (sm://, gs://, https://, file://) — fetch it
#      every rotate tick. Used by VMs that keep the key in Secret
#      Manager or similar for VM-replace resilience (legacy pattern,
#      foundryai-poc today).
#
#   2. TLS_PRIVKEY_URL empty AND $CERT_DIR/privkey.pem already on disk
#      — reuse the on-disk key, never fetch. The file survives the VM
#      for the lifetime of /data's persistence.
#
#   3. TLS_PRIVKEY_URL empty AND no on-disk key — generate an RSA-2048
#      key + a CSR against $DOMAIN in place. This is the "fresh VM"
#      bring-up path: the key never leaves the VM, and the CSR is
#      written to $CERT_DIR/cert.csr for the operator to grab via
#      `gcloud compute ssh … sudo cat /data/state/certs/cert.csr` and
#      attach to the SECURITY Jira that requests public-cert signing.
#      Until Security publishes the real fullchain, the self-signed
#      fallback below keeps Caddy serving HTTPS against this same key.
if [ -n "${TLS_PRIVKEY_URL:-}" ]; then
  if ! refetch "$TLS_PRIVKEY_URL" "$CERT_DIR/privkey.pem" 600 key; then
    if [ ! -s "$CERT_DIR/privkey.pem" ]; then
      echo "ERROR: privkey fetch failed and no cached copy exists — aborting" >&2
      exit 1
    fi
    echo "$(date -Is) privkey fetch failed; keeping cached $CERT_DIR/privkey.pem"
  fi
elif [ ! -s "$CERT_DIR/privkey.pem" ]; then
  CN="${DOMAIN:-localhost}"
  # Site-specific CSR subject (C/ST/L/O fields) comes from
  # TLS_CSR_SUBJECT in /opt/agnes/.env — the deployer's infra layer
  # writes it with its PKI conventions. This script stays generic;
  # default to a minimal /CN=<hostname> when the var is unset so the
  # CSR is still syntactically valid but carries no org metadata the
  # deployer didn't choose.
  SUBJECT="${TLS_CSR_SUBJECT:-/CN=$CN}"
  echo "$(date -Is) no privkey — generating RSA-2048 key + CSR (subject: $SUBJECT)"
  CSR_CONF=$(mktemp)
  cat > "$CSR_CONF" <<CFG
[ req ]
prompt             = no
distinguished_name = req_distinguished_name
req_extensions     = ext

[ req_distinguished_name ]
CN = $CN

[ ext ]
keyUsage         = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = @subject_alt_names

[ subject_alt_names ]
DNS.1 = $CN
CFG
  umask 077
  openssl req -newkey rsa:2048 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out "$CERT_DIR/cert.csr" \
    -subj "$SUBJECT" \
    -config "$CSR_CONF" -extensions ext -nodes 2>/dev/null
  chmod 600 "$CERT_DIR/privkey.pem"
  chmod 644 "$CERT_DIR/cert.csr"
  rm -f "$CSR_CONF"
  echo "$(date -Is) privkey.pem + cert.csr written to $CERT_DIR"
  echo "$(date -Is) ACTION: send $CERT_DIR/cert.csr to your certificate authority for signing — the CSR is public and safe to transit; the key never leaves this VM."
fi

# Real cert fetch. On failure, fall back to self-signed IFF no
# fullchain exists yet. If one exists (prior real OR prior self-signed)
# keep it — a transient fetch failure should not churn certs.
if ! refetch "$TLS_FULLCHAIN_URL" "$CERT_DIR/fullchain.pem" 644 cert; then
  if [ ! -s "$CERT_DIR/fullchain.pem" ]; then
    echo "$(date -Is) real cert unavailable at $TLS_FULLCHAIN_URL — generating 30-day self-signed"
    if [ ! -s "$CERT_DIR/privkey.pem" ]; then
      echo "ERROR: no privkey available — cannot self-sign" >&2
      exit 1
    fi
    CN="${DOMAIN:-localhost}"
    # Same parametrisation as the CSR branch above — site-specific PKI
    # fields belong in the deployer's .env, not in this script. Keeps
    # the self-signed bring-up cert consistent with whatever the eventual
    # CA-signed cert will say.
    SUBJECT="${TLS_CSR_SUBJECT:-/CN=$CN}"
    openssl req -x509 -new -key "$CERT_DIR/privkey.pem" \
      -out "$CERT_DIR/fullchain.pem" -days 30 \
      -subj "$SUBJECT" \
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

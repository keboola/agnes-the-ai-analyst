#!/usr/bin/env bash
# M-tier smoke: boots the role-split profile, asserts probes + kill-one-api
# continuity. Local/dev harness — needs docker; not run in unit CI.
set -euo pipefail
cd "$(dirname "$0")/../.."

export JWT_SECRET_KEY="${JWT_SECRET_KEY:-$(openssl rand -hex 32)}"
export SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"
# Required by docker-compose.postgres.yml's postgres side-car — no literal
# default there by design (LOW-2), so the harness must supply one.
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 20)}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.mtier.yml --profile mtier)

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${COMPOSE[@]}" up -d --build
for i in $(seq 1 60); do
  curl -fsS localhost:8080/readyz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS localhost:8080/healthz | grep -q alive || { echo "FAIL healthz"; exit 1; }
curl -fsS localhost:8080/readyz | grep -q ready || { echo "FAIL readyz"; exit 1; }

echo "killing api1 under traffic..."
"${COMPOSE[@]}" kill api1
fails=0
for i in $(seq 1 20); do
  curl -fsS -m 2 localhost:8080/healthz >/dev/null 2>&1 || fails=$((fails+1))
  sleep 0.5
done
[ "$fails" -le 2 ] || { echo "FAIL: $fails/20 requests failed after killing api1"; exit 1; }
echo "MTIER SMOKE OK (failures after kill: $fails/20)"

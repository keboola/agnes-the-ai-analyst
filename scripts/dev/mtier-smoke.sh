#!/usr/bin/env bash
# M-tier smoke: boots the role-split profile, asserts probes + kill-one-api
# continuity + redis coordination plumbing + FLUSHALL chaos resilience.
# Local/dev harness — needs docker; not run in unit CI.
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

AUTH_TOKEN_PAYLOAD='{"email":"smoke-nobody@example.com","password":"wrong"}'

# Fire one throwaway POST /auth/token (bad creds -> 401, but slowapi's
# @limiter.limit decorator increments the rate-limit bucket *before* the
# handler runs, so the request still counts). Ignored on purpose — used
# only to make the auth rate limiter touch redis.
hit_auth_token() {
  curl -fsS -m 2 -o /dev/null -X POST localhost:8080/auth/token \
    -H 'Content-Type: application/json' -d "$AUTH_TOKEN_PAYLOAD" >/dev/null 2>&1 || true
}

readyz_is_ready() {
  curl -fsS -m 2 localhost:8080/readyz 2>/dev/null | grep -Eq '"status":[[:space:]]*"ready"'
}

# --- 1. Config plumbing: redis URL must reach every role container ------
# coordination.backend=redis (config/instance.mtier.yaml) but
# resolve_redis_url() (app/coordination/factory.py) reads AGNES_REDIS_URL
# (env, checked first) or redis.url (instance.yaml), falling back to
# redis://localhost:6379/0 if neither is set. Inside a container
# "localhost" is the container itself, not the compose `redis` service —
# a missing override here would silently break every redis-backed
# coordination call (leases, and app/auth/rate_limit.py's rate-limiter
# storage) without failing fast anywhere. Static check on the compose
# overlay, before anything boots.
echo "checking redis URL plumbing in docker-compose.mtier.yml..."
for role in api1 api2 gateway worker; do
  awk -v role="  ${role}:" '
    $0 == role { in_block=1; next }
    in_block && /^  [a-zA-Z0-9_-]+:$/ { in_block=0 }
    in_block { print }
  ' docker-compose.mtier.yml | grep -q 'AGNES_REDIS_URL: redis://redis:' \
    || { echo "FAIL: ${role} stanza missing AGNES_REDIS_URL pointing at the compose redis service"; exit 1; }
done
echo "redis URL plumbing OK (statically, in the compose overlay)"

"${COMPOSE[@]}" up -d --build
for i in $(seq 1 60); do
  curl -fsS localhost:8080/readyz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS localhost:8080/healthz | grep -q alive || { echo "FAIL healthz"; exit 1; }
curl -fsS localhost:8080/readyz | grep -q ready || { echo "FAIL readyz"; exit 1; }

# Dynamic confirmation that the env actually resolved inside a live
# container — `extends:`/`profiles:` merging can silently drop keys (see
# the api1/api2 anchor-vs-explicit-stanza comment in
# docker-compose.mtier.yml), so the static grep above isn't sufficient on
# its own.
for role in api1 api2 gateway worker; do
  "${COMPOSE[@]}" exec -T "$role" printenv AGNES_REDIS_URL 2>/dev/null | grep -q '^redis://redis:' \
    || { echo "FAIL: ${role} container does not see AGNES_REDIS_URL pointing at the compose redis service"; exit 1; }
done
echo "redis URL plumbing OK (confirmed live inside api1/api2/gateway/worker)"

# --- 2. Coordination-live assertion --------------------------------------
echo "checking redis reachability..."
"${COMPOSE[@]}" exec -T redis redis-cli PING | grep -q PONG || { echo "FAIL: redis PING"; exit 1; }

# Real lease/ws-ticket keys (app/coordination/leases.py's
# `paused-sandbox-sweep`; app/api/chat.py's `ws-ticket:<id>`) only ever
# appear once chat is enabled (chat.enabled=true plus ANTHROPIC_API_KEY,
# E2B_API_KEY and chat.e2b_template_id — see app/main.py's
# `_chat_*_ok` startup guards), none of which this harness configures
# (config/instance.mtier.yaml has no `chat:` section at all). Forcing
# that on would pull in an E2B/Anthropic dependency this smoke doesn't
# otherwise need just to get a key to scan for. Same reason: the
# gateway-kill→sweep-lease-reappears continuity check is intentionally
# omitted (sweep lease only fires when chat.enabled=true).
#
# Instead: app/auth/rate_limit.py points slowapi's Limiter storage at the
# exact same resolve_redis_url() the coordination backend itself uses,
# and it is always active (AGNES_AUTH_RATELIMIT_ENABLED defaults on,
# unset here) regardless of chat config. POST /auth/token a few times
# (bad creds -> 401, but the bucket still increments) and confirm a
# `limits`-library key lands in redis — its storage backend prefixes
# every key with "LIMITS:" (see limits.storage.redis.RedisStorage.PREFIX
# in the vendored dependency). This is the most robust real observable of
# the redis coordination wiring working end-to-end through the running
# app, without depending on optional chat/Slack/Telegram features.
hit_auth_token
hit_auth_token
hit_auth_token
coord_ok=0
for i in $(seq 1 45); do
  count=$("${COMPOSE[@]}" exec -T redis redis-cli --scan --pattern 'LIMITS:*' 2>/dev/null | wc -l | tr -d ' ' || true)
  if [ "${count:-0}" -gt 0 ]; then coord_ok=1; break; fi
  sleep 2
done
[ "$coord_ok" -eq 1 ] || { echo "FAIL: no LIMITS:* keys appeared in redis within 90s of boot (coordination redis path not wired)"; exit 1; }
echo "coordination-live OK (redis carries rate-limiter state; LIMITS:* keys=$count)"

# --- 2.5 Prometheus scrape confirmation ----------------------------------
# Wave 2D (observability): deploy/prometheus/prometheus.yml polls every
# role container's `/metrics` (app/observability/metrics.py) every 15s by
# Compose DNS name (api1/api2/gateway/worker:8000, plus cadvisor:8080).
# Confirm via `up{job=~"agnes.*"}` on the Prometheus HTTP API, exec'd from
# inside the compose network (the app image already has curl — see
# Dockerfile) against the `prometheus` service's own DNS name, rather than
# the host-published :9090 port — this also exercises the same Compose
# DNS resolution the scrape targets themselves rely on, not just host
# port forwarding.
echo "checking prometheus scraped at least one agnes role target..."
prom_ok=0
for i in $(seq 1 30); do
  resp=$("${COMPOSE[@]}" exec -T gateway curl -fsS -m 3 -G 'http://prometheus:9090/api/v1/query' \
    --data-urlencode 'query=up{job=~"agnes.*"}' 2>/dev/null || true)
  if printf '%s' "$resp" | grep -q '"value":\[[0-9.]*,"1"\]'; then
    prom_ok=1
    break
  fi
  sleep 2
done
[ "$prom_ok" -eq 1 ] || { echo "FAIL: no agnes-* prometheus target reported up==1 within 60s of boot"; exit 1; }
echo "prometheus scrape OK (an agnes-* target reports up==1)"

echo "killing api1 under traffic..."
"${COMPOSE[@]}" kill api1
fails=0
for i in $(seq 1 20); do
  curl -fsS -m 2 localhost:8080/healthz >/dev/null 2>&1 || fails=$((fails+1))
  sleep 0.5
done
[ "$fails" -le 2 ] || { echo "FAIL: $fails/20 requests failed after killing api1"; exit 1; }
echo "api1 kill-continuity OK (failures: $fails/20)"

echo "restarting api1 before FLUSHALL chaos test..."
"${COMPOSE[@]}" up -d api1
for i in $(seq 1 60); do
  curl -fsS localhost:8080/readyz >/dev/null 2>&1 && break
  sleep 2
done

# --- 3. FLUSHALL chaos ----------------------------------------------------
# app/coordination/leases.py's module docstring calls this out explicitly
# ("FLUSHALL story"): losing all coordination state must never crash a
# replica or wedge the LB — at worst a lease/rate-limit bucket resets
# early, and the next acquire/increment starts clean. Drive real traffic
# (mixing a plain liveness probe with the redis-backed /auth/token path)
# straight through the wipe and verify that story holds.
echo "FLUSHALL chaos: wiping redis under traffic..."
flushall_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
loop_fails=0
for i in $(seq 1 20); do
  curl -fsS -m 2 localhost:8080/healthz >/dev/null 2>&1 || loop_fails=$((loop_fails+1))
  hit_auth_token
  if [ "$i" -eq 5 ]; then
    "${COMPOSE[@]}" exec -T redis redis-cli FLUSHALL >/dev/null
  fi
  sleep 0.5
done
[ "$loop_fails" -le 2 ] || { echo "FAIL: $loop_fails/20 healthz requests failed across the FLUSHALL"; exit 1; }
echo "FLUSHALL traffic-continuity OK (failures: $loop_fails/20)"

echo "waiting for healthz/readyz to be green post-FLUSHALL..."
recovered=0
for i in $(seq 1 15); do
  if curl -fsS -m 2 localhost:8080/healthz 2>/dev/null | grep -q alive && readyz_is_ready; then
    recovered=1
    break
  fi
  sleep 2
done
[ "$recovered" -eq 1 ] || { echo "FAIL: healthz/readyz did not recover green within 30s of FLUSHALL"; exit 1; }
echo "post-FLUSHALL healthz/readyz OK"

echo "checking api1/api2/gateway/worker logs for new tracebacks since FLUSHALL..."
tb_count=$("${COMPOSE[@]}" logs --since "$flushall_ts" api1 api2 gateway worker 2>/dev/null \
  | grep -c "Traceback (most recent call last)" || true)
if [ "${tb_count:-0}" -ne 0 ]; then
  echo "FAIL: $tb_count traceback(s) in api1/api2/gateway/worker logs since the FLUSHALL"
  "${COMPOSE[@]}" logs --since "$flushall_ts" api1 api2 gateway worker 2>/dev/null | grep -A20 "Traceback (most recent call last)" | head -100
  exit 1
fi
echo "no new tracebacks after FLUSHALL OK"

echo "verifying a fresh coordination write lands in redis after FLUSHALL..."
hit_auth_token
hit_auth_token
fresh_ok=0
for i in $(seq 1 15); do
  fresh_count=$("${COMPOSE[@]}" exec -T redis redis-cli --scan --pattern 'LIMITS:*' 2>/dev/null | wc -l | tr -d ' ' || true)
  if [ "${fresh_count:-0}" -gt 0 ]; then fresh_ok=1; break; fi
  sleep 1
done
[ "$fresh_ok" -eq 1 ] || { echo "FAIL: no fresh LIMITS:* key reappeared in redis after FLUSHALL"; exit 1; }
echo "post-FLUSHALL coordination write OK (redis repopulated within ${i}s; keys=$fresh_count)"

echo "MTIER SMOKE OK (kill failures: $fails/20, flushall failures: $loop_fails/20, post-flushall tracebacks: ${tb_count:-0})"

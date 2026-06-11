#!/bin/bash
# Agnes host-side watchdog — checks the container fleet every 5 minutes for
# the failure signatures of known production incidents:
#
#   - DuckDB FatalException crash loops ("terminate called" — the process
#     aborts from a worker thread, so no Python traceback exists)
#   - the invalidated-database "zombie" state ("database has been
#     invalidated" — the app still answers /api/health 200 while every
#     write returns 500, invisible to uptime checks)
#   - WAL salvage events ("WAL replay failed" + new *.wal.discarded.* files
#     — each one is a window of lost writes)
#   - ART index desync symptoms ("Failed to delete all rows from index",
#     "Failed to append to PRIMARY_*")
#   - container restart bursts, cgroup OOM kills, scheduler HTTP-500
#     streaks, /data disk pressure, dead health endpoint
#
# Alerts go to journald (`logger -t agnes-watchdog`) and
# /var/log/agnes-watchdog.log, plus an optional webhook (Slack / Google
# Chat compatible: POST {"text": "..."}). Configure in
# /etc/agnes-watchdog.env:
#
#   WEBHOOK_URL="https://..."   # empty = log-only
#   ENV_STAGE="prod"            # written by provisioning; drives the label
#   ENV_LABEL="..."             # optional full override of the label
#
# Runs as root via agnes-watchdog.timer. Deliberately independent of the
# app: its job is to report states in which the app can no longer report
# on itself.
set -u

STATE=/var/lib/agnes-watchdog
mkdir -p "$STATE"
[ -f /etc/agnes-watchdog.env ] && . /etc/agnes-watchdog.env
WEBHOOK_URL="${WEBHOOK_URL:-}"
HOST=$(hostname)

# Environment label precedence: explicit ENV_LABEL > ENV_STAGE (written by
# provisioning, e.g. the Terraform module's per-VM role) >
# POSTHOG_ENVIRONMENT from /opt/agnes/.env (deployments that set it) >
# hostname only.
STAGE="${ENV_STAGE:-}"
if [ -z "$STAGE" ]; then
    STAGE=$(grep -E '^POSTHOG_ENVIRONMENT=' /opt/agnes/.env 2>/dev/null | cut -d= -f2 | tr -d '"')
fi
case "$STAGE" in
  prod*) EMOJI=$(printf '\xf0\x9f\x94\xb4');;
  dev*)  EMOJI=$(printf '\xf0\x9f\x9f\xa1');;
  *)     EMOJI=$(printf '\xe2\x9a\xaa'); STAGE="${STAGE:-unknown}";;
esac
LABEL="${ENV_LABEL:-$EMOJI ${STAGE^^} ($HOST)}"

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SINCE=$(cat "$STATE/last_run" 2>/dev/null || date -u -d '-5 min' +%Y-%m-%dT%H:%M:%SZ)
echo "$NOW" > "$STATE/last_run"

ALERTS=()
add() { ALERTS+=("$1"); }

APP=agnes-app-1
LOGS=$(docker logs "$APP" --since "$SINCE" 2>&1)

c=$(grep -c "terminate called" <<<"$LOGS")
[ "$c" -gt 0 ] && add "CRASH: ${c}x 'terminate called' (DuckDB FatalException) since $SINCE"
c=$(grep -c "database has been invalidated" <<<"$LOGS")
[ "$c" -gt 0 ] && add "ZOMBIE: ${c}x 'database has been invalidated' — writes failing while app looks healthy"
c=$(grep -c "WAL replay failed" <<<"$LOGS")
[ "$c" -gt 0 ] && add "WAL-SALVAGE: ${c}x 'WAL replay failed' — possible data-loss window"
c=$(grep -c "Failed to delete all rows from index" <<<"$LOGS")
[ "$c" -gt 0 ] && add "INDEX-DESYNC: ${c}x 'Failed to delete all rows from index'"
c=$(grep -c "Failed to append to PRIMARY_" <<<"$LOGS")
[ "$c" -gt 0 ] && add "INDEX-APPEND-FATAL: ${c}x 'Failed to append to PRIMARY_*'"

newdisc=$(find /data/state -maxdepth 1 -name "*.wal.discarded.*" -newermt "$SINCE" 2>/dev/null)
[ -n "$newdisc" ] && add "NEW DISCARDED WAL: $newdisc"

rc=$(docker inspect "$APP" --format '{{.RestartCount}}' 2>/dev/null || echo "")
if [ -n "$rc" ]; then
    prev_rc=$(cat "$STATE/rc" 2>/dev/null || echo "$rc")
    echo "$rc" > "$STATE/rc"
    [ "$rc" -gt "$prev_rc" ] && add "RESTARTS: container RestartCount $prev_rc -> $rc"
else
    add "CONTAINER: $APP not inspectable (down?)"
fi

s500=$(docker logs agnes-scheduler-1 --since "$SINCE" 2>&1 | grep -c "HTTP 500")
[ "$s500" -ge 3 ] && add "SCHEDULER: ${s500} job calls returned HTTP 500 since $SINCE"

cid=$(docker inspect "$APP" --format '{{.Id}}' 2>/dev/null || echo "")
if [ -n "$cid" ] && [ -f "/sys/fs/cgroup/system.slice/docker-$cid.scope/memory.events" ]; then
    ook=$(awk '/^oom_kill /{print $2}' "/sys/fs/cgroup/system.slice/docker-$cid.scope/memory.events")
    prev_ook=$(cat "$STATE/oomk" 2>/dev/null || echo "$ook")
    echo "$ook" > "$STATE/oomk"
    [ "$ook" -gt "$prev_ook" ] && add "OOM: oom_kill counter $prev_ook -> $ook"
fi

duse=$(df --output=pcent /data 2>/dev/null | tail -1 | tr -dc 0-9)
[ -n "$duse" ] && [ "$duse" -ge 85 ] && add "DISK: /data at ${duse}%"

if ! docker exec "$APP" curl -sf -m 10 http://localhost:8000/api/health >/dev/null 2>&1; then
    add "HEALTH: /api/health not returning 200"
fi

[ "${#ALERTS[@]}" -eq 0 ] && exit 0

MSG="[agnes-watchdog] $LABEL | $NOW
$(printf ' - %s\n' "${ALERTS[@]}")"
logger -t agnes-watchdog "$MSG"
echo "$MSG" >> /var/log/agnes-watchdog.log

# Anti-spam: an identical alert set repeats on the webhook at most hourly
# (the journal + log file still record every occurrence).
H=$(printf '%s' "${ALERTS[*]}" | md5sum | cut -d' ' -f1)
LAST_H=$(cat "$STATE/alert_hash" 2>/dev/null || echo "")
LAST_T=$(cat "$STATE/alert_time" 2>/dev/null || echo 0)
NOW_E=$(date +%s)
if [ "$H" = "$LAST_H" ] && [ $((NOW_E - LAST_T)) -lt 3600 ]; then exit 0; fi
echo "$H" > "$STATE/alert_hash"; echo "$NOW_E" > "$STATE/alert_time"

if [ -n "$WEBHOOK_URL" ]; then
    esc=$(printf '%s' "$MSG" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')
    curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"text\": \"$esc\"}" "$WEBHOOK_URL" >/dev/null 2>&1 \
        || logger -t agnes-watchdog "webhook send failed"
fi
exit 0

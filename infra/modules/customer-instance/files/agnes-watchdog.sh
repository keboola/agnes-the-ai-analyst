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
# Besides incident alerts it also reports two informational deployment-
# timeline events (prefixed ' i ' in the message body): an app image
# change (auto-upgrade recreated the container) and a DB schema-version
# change (startup self-migration or a manual alembic run), both tracked
# as run-to-run deltas in the state dir.
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
# Informational deployment-timeline events (image upgrade, DB schema bump).
# Separate channel from ALERTS: they ride along with alerts or go out on
# their own, but never trip the hourly alert-type anti-spam (each is
# one-shot by construction — the underlying value changed).
INFOS=()
info() { INFOS+=("$1"); }

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

# Deployment timeline: report an app image change (auto-upgrade recreated
# the container with a new build). First run seeds state silently.
img=$(docker inspect "$APP" --format '{{.Image}}' 2>/dev/null || echo "")
if [ -n "$img" ]; then
    prev_img=$(cat "$STATE/image" 2>/dev/null || echo "")
    echo "$img" > "$STATE/image"
    if [ -n "$prev_img" ] && [ "$img" != "$prev_img" ]; then
        # Best-effort version context from the boot banner in the log window.
        ver=$(grep -oE 'Agnes [0-9][0-9.]* \| channel: [a-z-]* \| schema v[0-9]*' <<<"$LOGS" | tail -1)
        info "UPGRADE: app image ${prev_img#sha256:} -> ${img#sha256:}${ver:+ ($ver)}"
    fi
fi

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

health_body=$(docker exec "$APP" curl -sf -m 10 http://localhost:8000/api/health 2>/dev/null || echo "")
if [ -z "$health_body" ]; then
    add "HEALTH: /api/health not returning 200"
else
    # Deployment timeline: report a DB schema-version change (startup
    # self-migration / manual alembic run) from the health body the
    # liveness probe already fetched — no extra DB access. First run
    # seeds state silently.
    schema=$(grep -o '"current":[0-9]*' <<<"$health_body" | head -1 | tr -dc 0-9)
    if [ -n "$schema" ]; then
        prev_schema=$(cat "$STATE/schema" 2>/dev/null || echo "")
        echo "$schema" > "$STATE/schema"
        if [ -n "$prev_schema" ] && [ "$schema" != "$prev_schema" ]; then
            info "DB: schema v$prev_schema -> v$schema"
        fi
    fi
fi

[ "${#ALERTS[@]}" -eq 0 ] && [ "${#INFOS[@]}" -eq 0 ] && exit 0

BODY=""
[ "${#ALERTS[@]}" -gt 0 ] && BODY="$(printf ' - %s\n' "${ALERTS[@]}")"
if [ "${#INFOS[@]}" -gt 0 ]; then
    [ -n "$BODY" ] && BODY="$BODY
"
    BODY="$BODY$(printf ' i %s\n' "${INFOS[@]}")"
fi
MSG="[agnes-watchdog] $LABEL | $NOW
$BODY"
logger -t agnes-watchdog "$MSG"
echo "$MSG" >> /var/log/agnes-watchdog.log

# Anti-spam: the same set of alert TYPES repeats on the webhook at most
# hourly (the journal + log file still record every occurrence). Hash only
# the type prefix of each alert (text before the first colon), one per
# line — the message bodies embed per-run counts and the $SINCE timestamp,
# which would make every hash unique and the suppression a no-op; and
# joining the set into one line would truncate it to the first prefix and
# over-suppress distinct alert sets.
# Info lines bypass the suppression entirely: a run carrying any info is
# always sent (the info is one-shot), and an info-only run leaves the
# alert hash/time untouched so it cannot reset an active suppression.
if [ "${#ALERTS[@]}" -gt 0 ]; then
    H=$(printf '%s\n' "${ALERTS[@]}" | sed 's/:.*//' | md5sum | cut -d' ' -f1)
    LAST_H=$(cat "$STATE/alert_hash" 2>/dev/null || echo "")
    LAST_T=$(cat "$STATE/alert_time" 2>/dev/null || echo 0)
    NOW_E=$(date +%s)
    if [ "$H" = "$LAST_H" ] && [ $((NOW_E - LAST_T)) -lt 3600 ] && [ "${#INFOS[@]}" -eq 0 ]; then
        exit 0
    fi
    echo "$H" > "$STATE/alert_hash"; echo "$NOW_E" > "$STATE/alert_time"
fi

if [ -n "$WEBHOOK_URL" ]; then
    esc=$(printf '%s' "$MSG" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')
    curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"text\": \"$esc\"}" "$WEBHOOK_URL" >/dev/null 2>&1 \
        || logger -t agnes-watchdog "webhook send failed"
fi
exit 0

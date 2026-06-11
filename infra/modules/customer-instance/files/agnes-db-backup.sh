#!/bin/bash
# Daily system.duckdb backup + canary restore-verification.
#
# Copies the live DB file + WAL to /data/backups/system-duckdb/<YYYYMMDD>/
# and then PROVES the copy is restorable: agnes-db-verify.py opens a scratch
# copy (replaying the WAL), checks row counts, and exercises the statement
# classes that failed during the 2026-06 index-corruption incident. A backup
# that exists but cannot be opened is not a backup — and a failing verify is
# also the earliest available signal of silent on-disk corruption.
#
# Complements (does not replace) disk-level PD snapshots: a snapshot
# preserves a corrupted file faithfully; this catches the corruption.
#
# Keeps 7 daily backups. FAILED verify -> webhook alert (shared config in
# /etc/agnes-watchdog.env). Runs as root via agnes-db-backup.timer.
set -u
[ -f /etc/agnes-watchdog.env ] && . /etc/agnes-watchdog.env
WEBHOOK_URL="${WEBHOOK_URL:-}"
HOST=$(hostname)
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

TS=$(date -u +%Y%m%d)
ROOT=/data/backups/system-duckdb
DEST=$ROOT/$TS
mkdir -p "$DEST"
cp /data/state/system.duckdb "$DEST/system.duckdb"
[ -f /data/state/system.duckdb.wal ] && cp /data/state/system.duckdb.wal "$DEST/system.duckdb.wal"
# uid:gid 999:999 = the container's non-root user; the verify step runs
# inside the app container (where a known-good duckdb is installed).
chown -R 999:999 "$ROOT"

if docker exec agnes-app-1 python /data/backups/agnes-db-verify.py \
        "$DEST/system.duckdb" > "$DEST/verify.log" 2>&1; then
    STATUS=OK
else
    STATUS=FAILED
fi
echo "$STATUS $(date -u +%FT%TZ)" > "$DEST/STATUS"

find "$ROOT" -maxdepth 1 -type d -name '20*' -mtime +7 -exec rm -rf {} \;

MSG="[agnes-db-backup] $LABEL | $TS verify=$STATUS size=$(du -sh "$DEST" | cut -f1)"
logger -t agnes-db-backup "$MSG"
echo "$MSG" >> /var/log/agnes-watchdog.log
if [ "$STATUS" = "FAILED" ] && [ -n "$WEBHOOK_URL" ]; then
    curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"text\": \"$MSG — see $DEST/verify.log\"}" "$WEBHOOK_URL" >/dev/null 2>&1
fi
[ "$STATUS" = "OK" ]

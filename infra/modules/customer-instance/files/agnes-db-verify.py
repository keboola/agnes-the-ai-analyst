"""Canary restore-verification for a system.duckdb backup.

Run inside the app container by agnes-db-backup.sh:

    python /data/backups/agnes-db-verify.py <backup>/system.duckdb

Copies the backup (plus its .wal, if any) to a scratch dir so the backup
itself stays pristine, opens the copy — which replays the WAL — checks row
counts on the core tables, and exercises the two statement classes that
failed during the 2026-06 on-disk index-corruption incident (an
INSERT OR REPLACE that must delete index entries, and the rollup
DELETE+INSERT that must re-append them), inside a transaction that is
rolled back. Any exception -> non-zero exit -> the caller marks the backup
FAILED and alerts.

A passing run proves two things at once: the backup is restorable, and the
live file it was copied from has internally consistent indexes on the
exercised tables.
"""

import os
import shutil
import sys
import tempfile

import duckdb

src = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="dbverify-")
db = os.path.join(tmp, "verify.duckdb")
shutil.copy(src, db)
if os.path.exists(src + ".wal"):
    shutil.copy(src + ".wal", db + ".wal")

con = duckdb.connect(db)
for t in [
    "users",
    "table_registry",
    "resource_grants",
    "user_groups",
    "user_group_members",
    "usage_session_summary",
    "usage_tool_daily",
    "usage_events",
    "audit_log",
    "knowledge_items",
]:
    print(f"{t}: {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")

con.execute("BEGIN")
con.execute("INSERT OR REPLACE INTO usage_session_summary SELECT * FROM usage_session_summary LIMIT 5")
con.execute("DELETE FROM usage_tool_daily WHERE day >= current_date - INTERVAL 7 DAY")
con.execute(
    """INSERT INTO usage_tool_daily
        (day, tool_name, source, invocations, error_count,
         distinct_users, distinct_sessions)
      SELECT CAST(occurred_at AS DATE), tool_name, source, COUNT(*),
             SUM(CASE WHEN is_error THEN 1 ELSE 0 END),
             COUNT(DISTINCT username), COUNT(DISTINCT session_id)
      FROM usage_events
      WHERE CAST(occurred_at AS DATE) >= current_date - INTERVAL 7 DAY
        AND tool_name IS NOT NULL
      GROUP BY 1, 2, 3"""
)
con.execute("ROLLBACK")
con.close()
shutil.rmtree(tmp)
print("VERIFY OK")

"""
Jira connector - optional push-based data integration.

Provides real-time webhook ingestion, batch backfill, SLA polling,
and incremental Parquet transforms for Jira Cloud issues.

Enable by setting jira.enabled: true in config/instance.yaml
and providing JIRA_* environment variables.
"""

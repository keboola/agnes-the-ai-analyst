"""Tests for Jira extract_init — init and update_meta."""
import duckdb
import pytest
from pathlib import Path
from connectors.jira.extract_init import init_extract, update_meta


@pytest.fixture
def jira_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    jira_dir = tmp_path / "extracts" / "jira"
    jira_dir.mkdir(parents=True)
    return jira_dir


class TestJiraExtractInit:
    def test_init_creates_extract_db(self, jira_env):
        init_extract(jira_env)
        assert (jira_env / "extract.duckdb").exists()
        conn = duckdb.connect(str(jira_env / "extract.duckdb"))
        meta = conn.execute("SELECT * FROM _meta").fetchall()
        conn.close()
        assert isinstance(meta, list)

    def test_update_meta_creates_view(self, jira_env):
        init_extract(jira_env)
        issues_dir = jira_env / "data" / "issues"
        issues_dir.mkdir(parents=True, exist_ok=True)
        pq_path = str(issues_dir / "2026-04.parquet")
        tmp = duckdb.connect()
        tmp.execute(f"COPY (SELECT 'PROJ-1' AS issue_key, 'Bug' AS type) TO '{pq_path}' (FORMAT PARQUET)")
        tmp.close()

        update_meta(jira_env, "issues")

        conn = duckdb.connect(str(jira_env / "extract.duckdb"))
        rows = conn.execute("SELECT rows FROM _meta WHERE table_name='issues'").fetchone()
        assert rows[0] == 1
        data = conn.execute("SELECT issue_key FROM issues").fetchone()
        assert data[0] == "PROJ-1"
        conn.close()

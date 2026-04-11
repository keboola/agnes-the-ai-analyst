"""Tests for CLI commands."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-cli-tests")
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path


class TestCLIHelp:
    def test_main_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "AI Data Analyst CLI" in result.output

    def test_auth_help(self):
        result = runner.invoke(app, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output

    def test_sync_help(self):
        result = runner.invoke(app, ["sync", "--help"])
        assert result.exit_code == 0

    def test_query_help(self):
        result = runner.invoke(app, ["query", "--help"])
        assert result.exit_code == 0

    def test_admin_help(self):
        result = runner.invoke(app, ["admin", "--help"])
        assert result.exit_code == 0

    def test_admin_metadata_help(self):
        result = runner.invoke(app, ["admin", "metadata-show", "--help"])
        assert result.exit_code == 0

    def test_diagnose_help(self):
        result = runner.invoke(app, ["diagnose", "--help"])
        assert result.exit_code == 0

    def test_skills_help(self):
        result = runner.invoke(app, ["skills", "--help"])
        assert result.exit_code == 0


class TestSkills:
    def test_list_skills(self):
        result = runner.invoke(app, ["skills", "list"])
        assert result.exit_code == 0
        assert "setup" in result.output
        assert "troubleshoot" in result.output

    def test_show_skill(self):
        result = runner.invoke(app, ["skills", "show", "setup"])
        assert result.exit_code == 0
        assert "Prerequisites" in result.output

    def test_show_nonexistent_skill(self):
        result = runner.invoke(app, ["skills", "show", "nonexistent"])
        assert result.exit_code == 1


class TestAuth:
    def test_whoami_not_logged_in(self):
        result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 1
        assert "Not logged in" in result.output

    def test_logout(self):
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_login_with_mock_server(self, tmp_config):
        """Test login against a real FastAPI test server."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        from argon2 import PasswordHasher
        conn = get_system_db()
        repo = UserRepository(conn)
        repo.create(id="u1", email="test@acme.com", name="Test", role="analyst",
                    password_hash=PasswordHasher().hash("testpass"))
        conn.close()

        from fastapi.testclient import TestClient
        from app.main import create_app
        test_app = create_app()

        with patch("cli.client.get_client") as mock_get_client:
            client = TestClient(test_app)
            mock_get_client.return_value.__enter__ = MagicMock(return_value=client)
            mock_get_client.return_value.__exit__ = MagicMock(return_value=False)

            # Simulate the API call
            resp = client.post("/auth/token", json={"email": "test@acme.com", "password": "testpass"})
            assert resp.status_code == 200
            token = resp.json()["access_token"]

            # Save token manually (since we can't easily mock typer prompts)
            from cli.config import save_token
            save_token(token, "test@acme.com", "analyst")

            # Now whoami should work
            result = runner.invoke(app, ["auth", "whoami"])
            assert result.exit_code == 0
            assert "test@acme.com" in result.output


class TestStatus:
    def test_local_status_empty(self):
        result = runner.invoke(app, ["status", "--local"])
        assert result.exit_code == 0
        assert "Tables synced: 0" in result.output

    def test_local_status_json(self):
        result = runner.invoke(app, ["status", "--local", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["mode"] == "local"


class TestQuery:
    def test_query_no_db(self, tmp_config):
        result = runner.invoke(app, ["query", "SELECT 1"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_query_with_db(self, tmp_config):
        import duckdb
        local_dir = tmp_config / "local"
        db_dir = local_dir / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE test_table (id INT, name VARCHAR)")
        conn.execute("INSERT INTO test_table VALUES (1, 'hello'), (2, 'world')")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT count(*) as cnt FROM test_table", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["cnt"] == 2


class TestAdminCommands:
    def test_register_table(self, tmp_config):
        """Test da admin register-table calls the API and reports success."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "tbl-1", "name": "orders"}

        with patch("cli.commands.admin.api_post", return_value=mock_resp) as mock_post:
            result = runner.invoke(app, [
                "admin", "register-table", "orders",
                "--source-type", "keboola",
                "--bucket", "in.c-crm",
                "--query-mode", "local",
            ])
            assert result.exit_code == 0
            assert "Registered: orders" in result.output
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "/api/admin/register-table"
            assert call_args[1]["json"]["name"] == "orders"

    def test_register_table_conflict(self, tmp_config):
        """Test da admin register-table when table already exists."""
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.json.return_value = {"detail": "Table already exists"}

        with patch("cli.commands.admin.api_post", return_value=mock_resp):
            result = runner.invoke(app, ["admin", "register-table", "orders"])
            assert result.exit_code == 0
            assert "Already exists: orders" in result.output

    def test_list_tables(self, tmp_config):
        """Test da admin list-tables returns table listing."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "count": 2,
            "tables": [
                {"name": "orders", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm", "id": "t1"},
                {"name": "customers", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm", "id": "t2"},
            ],
        }

        with patch("cli.commands.admin.api_get", return_value=mock_resp):
            result = runner.invoke(app, ["admin", "list-tables"])
            assert result.exit_code == 0
            assert "Registered tables: 2" in result.output
            assert "orders" in result.output
            assert "customers" in result.output

    def test_list_tables_json(self, tmp_config):
        """Test da admin list-tables --json outputs valid JSON."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "count": 1,
            "tables": [
                {"name": "orders", "source_type": "keboola", "query_mode": "local", "bucket": "in.c-crm", "id": "t1"},
            ],
        }

        with patch("cli.commands.admin.api_get", return_value=mock_resp):
            result = runner.invoke(app, ["admin", "list-tables", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["count"] == 1

    def test_list_tables_api_failure(self, tmp_config):
        """Test da admin list-tables handles API errors."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.json.return_value = {"detail": "Internal Server Error"}

        with patch("cli.commands.admin.api_get", return_value=mock_resp):
            result = runner.invoke(app, ["admin", "list-tables"])
            assert result.exit_code == 1


class TestMetricsHelp:
    def test_metrics_help(self):
        result = runner.invoke(app, ["metrics", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "import" in result.output

    def test_analyst_help(self):
        result = runner.invoke(app, ["analyst", "--help"])
        assert result.exit_code == 0
        assert "setup" in result.output

    def test_analyst_status_help(self):
        result = runner.invoke(app, ["analyst", "status", "--help"])
        assert result.exit_code == 0
        assert "freshness" in result.output.lower() or "workspace" in result.output.lower()

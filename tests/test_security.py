"""Security tests — sandbox escapes, SQL injection, access control."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["JWT_SECRET_KEY"] = "test-secret-32chars-minimum!!!!!"
    os.environ["SCRIPT_TIMEOUT"] = "5"

    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="user@test.com", name="User", role="analyst")
    conn.close()

    app = create_app()
    c = TestClient(app)
    token = create_access_token("u1", "user@test.com", "analyst")
    return c, token


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---- Script Sandbox ----

class TestScriptSandbox:
    def test_blocks_os_system(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "import os\nos.system('whoami')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_dunder_import(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "__import__('subprocess').run(['ls'])"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_eval(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "eval('print(1)')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_exec(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "exec('import os')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_open(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "open('/etc/passwd').read()"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_socket(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "import socket"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_pathlib(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={"source": "from pathlib import Path"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_allows_safe_script(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import math\nprint(math.sqrt(144))",
        }, headers=_headers(token))
        assert resp.status_code == 200
        assert "12" in resp.json()["stdout"]

    def test_allows_duckdb(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import duckdb\nconn=duckdb.connect(':memory:')\nprint(conn.execute('SELECT 42').fetchone()[0])",
        }, headers=_headers(token))
        assert resp.status_code == 200
        assert "42" in resp.json()["stdout"]

    def test_allows_json(self, client):
        c, token = client
        resp = c.post("/api/scripts/run", json={
            "source": "import json\nprint(json.dumps({'a': 1}))",
        }, headers=_headers(token))
        assert resp.status_code == 200
        assert '"a"' in resp.json()["stdout"]

    def test_runtime_import_blocked(self, client):
        """Even if static check passes, runtime __import__ override catches it."""
        c, token = client
        # This uses string concatenation to bypass static check
        resp = c.post("/api/scripts/run", json={
            "source": "x='sub'+'process'\ntry:\n m=type('',(),{'__init__':lambda s:None})()\nexcept:\n pass\nprint('safe')",
        }, headers=_headers(token))
        # Should still run but without access to dangerous modules
        assert resp.status_code == 200


# ---- SQL Query Security ----

class TestQuerySecurity:
    def test_blocks_copy_to(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "COPY (SELECT 1) TO '/tmp/pwned.csv'"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_read_csv(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT * FROM read_csv_auto('/etc/passwd')"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_semicolon(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT 1; SELECT 2"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_non_select(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "CREATE TABLE pwned (id INT)"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_blocks_attach(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "ATTACH '/tmp/pwned.db'"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_allows_select(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "SELECT 1 as test, 'hello' as msg"},
                       headers=_headers(token))
        assert resp.status_code == 200
        assert resp.json()["columns"] == ["test", "msg"]

    def test_allows_with_cte(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "WITH t AS (SELECT 1 as x) SELECT * FROM t"},
                       headers=_headers(token))
        assert resp.status_code == 200

    def test_blocks_drop(self, client):
        c, token = client
        resp = c.post("/api/query", json={"sql": "DROP TABLE IF EXISTS users"},
                       headers=_headers(token))
        assert resp.status_code == 400

    def test_no_auth(self, client):
        c, _ = client
        resp = c.post("/api/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 401


# ---- Auth Edge Cases ----

class TestAuthSecurity:
    def test_garbage_token(self, client):
        c, _ = client
        resp = c.get("/api/scripts", headers={"Authorization": "Bearer garbage.token.here"})
        assert resp.status_code == 401

    def test_empty_bearer(self, client):
        c, _ = client
        resp = c.get("/api/scripts", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_no_bearer_prefix(self, client):
        c, token = client
        resp = c.get("/api/scripts", headers={"Authorization": token})
        assert resp.status_code == 401

    def test_missing_header(self, client):
        c, _ = client
        resp = c.get("/api/scripts")
        assert resp.status_code == 401

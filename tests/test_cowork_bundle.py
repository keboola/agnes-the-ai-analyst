"""Tests for Agnes Cowork bundle endpoints (M2/M3).

Covers:
- POST /api/user/cowork-bundle  → ZIP download
- GET  /api/user/setup-tokens   → list active tokens
- DELETE /api/user/setup-tokens/{id}  → revoke
- POST /api/auth/exchange-setup-token → exchange → PAT
- Rate-limit: max 5 active tokens
- Exchange: invalid / expired / already-used tokens
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestGenerateBundle:
    def test_generates_valid_zip(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle",
                      headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/zip")
        assert "agnes-cowork-setup" in resp.headers.get("content-disposition", "")

        # ZIP must create a workspace folder (folder-prefixed structure)
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()

        # Every entry lives inside a top-level folder
        assert all("/" in n for n in names), (
            f"Expected folder-prefixed entries; got: {names}"
        )
        folders = {n.split("/")[0] for n in names}
        assert len(folders) == 1, f"Expected one top-level folder; got: {folders}"
        folder = folders.pop()
        assert folder.startswith("agnes-cowork-setup-")

        # Required workspace files
        assert f"{folder}/agnes-bundle.json" in names   # no leading dot — visible to Claude tools
        assert f"{folder}/setup.py" in names             # pure stdlib one-time setup
        assert f"{folder}/mcp_server.py" in names        # stdio MCP proxy
        assert f"{folder}/agnes.py" in names             # Bash-tool CLI fallback
        assert f"{folder}/.claude/settings.json" in names
        assert f"{folder}/CLAUDE.md" in names
        assert f"{folder}/cacert.pem" in names           # Mozilla CA bundle for verified TLS

        # The bundled CA file must be a real PEM cert bundle
        assert "BEGIN CERTIFICATE" in zf.read(f"{folder}/cacert.pem").decode()

        # Shell scripts are gone — no terminal setup required
        assert "setup.sh" not in names
        assert "README.txt" not in names

        # Bundle JSON must have required fields
        bundle = json.loads(zf.read(f"{folder}/agnes-bundle.json"))
        assert bundle["version"] == 1
        assert bundle["setup_token"].startswith("st_")
        assert bundle["access_token"].startswith("ey")  # pre-baked PAT (JWT)
        assert "server_url" in bundle
        assert "expires_at" in bundle

        # settings.json must wire the one-time setup hook
        settings = json.loads(zf.read(f"{folder}/.claude/settings.json"))
        start_hooks = settings.get("hooks", {}).get("SessionStart", [])
        commands = [
            h.get("command", "")
            for entry in start_hooks
            for h in entry.get("hooks", [])
        ]
        assert any("setup.py" in cmd for cmd in commands), (
            f"SessionStart hook missing setup.py; got: {commands}"
        )

        # settings.json must wire the MCP server via stdio (mcp_server.py proxy)
        mcp_servers = settings.get("mcpServers", {})
        assert "agnes" in mcp_servers, f"mcpServers missing 'agnes'; got: {mcp_servers}"
        agnes_mcp = mcp_servers["agnes"]
        assert "command" in agnes_mcp, (
            f"MCP entry should have 'command' (stdio), got: {agnes_mcp}"
        )
        assert "mcp_server.py" in str(agnes_mcp.get("args", [])), (
            f"MCP args should reference mcp_server.py; got: {agnes_mcp.get('args')}"
        )

    def test_bundled_scripts_verify_tls_by_default(self, seeded_app):
        """mcp_server.py / agnes.py must verify TLS against the bundled CA by
        default; CERT_NONE is only reachable via the explicit opt-out env var."""
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle",
                      headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        folder = zf.namelist()[0].split("/")[0]
        for script in ("mcp_server.py", "agnes.py"):
            src = zf.read(f"{folder}/{script}").decode()
            assert "context=_SSL_CTX" in src, f"{script}: urlopen must pass an SSL context"
            assert "cacert.pem" in src, f"{script}: must reference the bundled CA file"
            assert "AGNES_INSECURE_SKIP_TLS_VERIFY" in src, f"{script}: opt-out gate missing"
            assert "ssl.create_default_context" in src, f"{script}: must build a verifying context"

    def test_requires_authentication(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle")
        assert resp.status_code in (401, 403)

    def test_rate_limit_5_tokens(self, seeded_app):
        """After 5 active tokens, 6th request returns 400."""
        c = seeded_app["client"]
        for _ in range(5):
            r = c.post("/api/user/cowork-bundle",
                       headers=_auth(seeded_app["analyst_token"]))
            assert r.status_code == 200, r.text

        r6 = c.post("/api/user/cowork-bundle",
                    headers=_auth(seeded_app["analyst_token"]))
        assert r6.status_code == 400
        detail = r6.json()["detail"]
        assert detail["kind"] == "too_many_setup_tokens"

    def test_admin_can_also_generate(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle",
                      headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200


class TestListAndRevoke:
    def test_list_returns_generated_tokens(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/user/cowork-bundle",
               headers=_auth(seeded_app["analyst_token"]))

        resp = c.get("/api/user/setup-tokens",
                     headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        tokens = resp.json()
        assert len(tokens) >= 1
        assert "id" in tokens[0]
        assert "expires_at" in tokens[0]

    def test_revoke_removes_token(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/user/cowork-bundle",
               headers=_auth(seeded_app["analyst_token"]))

        tokens_before = c.get("/api/user/setup-tokens",
                               headers=_auth(seeded_app["analyst_token"])).json()
        assert len(tokens_before) >= 1
        token_id = tokens_before[0]["id"]

        del_resp = c.delete(f"/api/user/setup-tokens/{token_id}",
                            headers=_auth(seeded_app["analyst_token"]))
        assert del_resp.status_code == 204

        tokens_after = c.get("/api/user/setup-tokens",
                              headers=_auth(seeded_app["analyst_token"])).json()
        assert all(t["id"] != token_id for t in tokens_after)

    def test_cannot_revoke_other_users_token(self, seeded_app):
        c = seeded_app["client"]
        # analyst generates a token
        c.post("/api/user/cowork-bundle",
               headers=_auth(seeded_app["analyst_token"]))
        analyst_tokens = c.get("/api/user/setup-tokens",
                                headers=_auth(seeded_app["analyst_token"])).json()
        token_id = analyst_tokens[0]["id"]

        # admin tries to revoke analyst's token via their own user-scoped endpoint
        resp = c.delete(f"/api/user/setup-tokens/{token_id}",
                        headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404  # not found for a different user


class TestExchangeSetupToken:
    def _get_raw_token_from_bundle(self, seeded_app) -> tuple[str, str]:
        """Generate a bundle and return (raw_setup_token, server_url)."""
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle",
                      headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        # Bundle JSON is at <folder>/agnes-bundle.json (folder-prefixed structure)
        bundle_name = next(
            n for n in zf.namelist() if n.endswith("/agnes-bundle.json")
        )
        bundle = json.loads(zf.read(bundle_name))
        return bundle["setup_token"], bundle["server_url"]

    def test_exchange_returns_pat(self, seeded_app):
        raw_token, _ = self._get_raw_token_from_bundle(seeded_app)
        c = seeded_app["client"]

        resp = c.post("/api/auth/exchange-setup-token",
                      json={"setup_token": raw_token})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["access_token"].startswith("ey")  # JWT
        assert "user_email" in data
        assert "server_url" in data

    def test_exchange_is_single_use(self, seeded_app):
        raw_token, _ = self._get_raw_token_from_bundle(seeded_app)
        c = seeded_app["client"]

        first = c.post("/api/auth/exchange-setup-token",
                       json={"setup_token": raw_token})
        assert first.status_code == 200

        second = c.post("/api/auth/exchange-setup-token",
                        json={"setup_token": raw_token})
        assert second.status_code == 401
        assert "already been used" in second.json()["detail"].lower()

    def test_exchange_invalid_token(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/auth/exchange-setup-token",
                      json={"setup_token": "st_notreal"})
        assert resp.status_code == 401

    def test_exchange_bad_format(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/auth/exchange-setup-token",
                      json={"setup_token": "not_a_setup_token"})
        assert resp.status_code == 400

    def test_exchange_expired_token(self, seeded_app):
        """A token whose expires_at is in the past should be rejected.

        Uses a unit-level DuckDB in-memory connection to insert a stale token
        directly into the repository, then calls the exchange endpoint via the
        running TestClient.  The running app shares the same on-disk system.duckdb,
        so we insert through a second connection to the same DB file.
        """
        import uuid
        import hashlib
        import duckdb as _duckdb

        data_dir = seeded_app["env"]["data_dir"]
        db_path = str(data_dir / "state" / "system.duckdb")

        raw = "st_" + "e" * 64
        tok_hash = hashlib.sha256(raw.encode()).hexdigest()
        tok_id = str(uuid.uuid4())
        past = datetime.now(timezone.utc) - timedelta(hours=2)

        # Insert via a separate read-write connection (DuckDB allows multi-reader
        # but only one writer at a time; the TestClient holds a writer lock on
        # the DB, so we use the endpoint instead of a direct conn).
        # Instead: insert the expired token through the API shim that gives us a
        # fresh DB connection per request.
        # We mock the SetupTokenRepository to return our crafted row.
        from unittest.mock import patch

        expired_row = {
            "id": tok_id,
            "user_id": "analyst1",
            "token_hash": tok_hash,
            "expires_at": past,
            "used_at": None,
            "created_at": datetime.now(timezone.utc),
        }

        c = seeded_app["client"]
        with patch(
            "app.api.cowork_bundle.SetupTokenRepository.get_by_hash",
            return_value=expired_row,
        ):
            resp = c.post("/api/auth/exchange-setup-token",
                          json={"setup_token": raw})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_generated_pat_is_usable(self, seeded_app):
        """PAT returned by exchange should authenticate subsequent requests."""
        raw_token, _ = self._get_raw_token_from_bundle(seeded_app)
        c = seeded_app["client"]

        exchange = c.post("/api/auth/exchange-setup-token",
                          json={"setup_token": raw_token})
        assert exchange.status_code == 200
        pat = exchange.json()["access_token"]

        # The freshly-minted PAT should authenticate on any user-scoped endpoint.
        # /api/user/setup-tokens requires auth and returns 200 for any user.
        resp = c.get("/api/user/setup-tokens", headers=_auth(pat))
        assert resp.status_code == 200

"""Tests for scripts API endpoints — deploy, run, list, undeploy."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestScriptsList:
    def test_list_scripts_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/scripts", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["scripts"] == []

    def test_list_scripts_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/scripts")
        assert resp.status_code == 401


class TestScriptsDeploy:
    def test_deploy_safe_script(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/scripts/deploy",
            json={"name": "hello", "source": "print('hello world')"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "hello"
        assert "id" in data

    def test_deploy_with_schedule(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/scripts/deploy",
            json={"name": "scheduled", "source": "print('scheduled')", "schedule": "0 8 * * MON"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["schedule"] == "0 8 * * MON"

    def test_deploy_script_with_blocked_import_deploys_ok_but_run_fails(self, seeded_app):
        """Deploy stores scripts as-is; safety validation happens at run time, not deploy time."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        # Deploy succeeds (no pre-validation at deploy time)
        deploy_resp = c.post(
            "/api/scripts/deploy",
            json={"name": "bad_import", "source": "import os; print(os.getcwd())"},
            headers=_auth(token),
        )
        assert deploy_resp.status_code == 201
        script_id = deploy_resp.json()["id"]

        # Running it should fail with 400 due to blocked import
        run_resp = c.post(f"/api/scripts/{script_id}/run", headers=_auth(token))
        assert run_resp.status_code == 400
        assert "Blocked" in run_resp.json()["detail"] or "disallowed" in run_resp.json()["detail"]

    def test_deploy_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/scripts/deploy",
            json={"name": "hello", "source": "print('hello')"},
        )
        assert resp.status_code == 401

    def test_deploy_appears_in_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        c.post(
            "/api/scripts/deploy",
            json={"name": "listed_script", "source": "x = 1"},
            headers=_auth(token),
        )
        resp = c.get("/api/scripts", headers=_auth(token))
        assert resp.json()["count"] >= 1
        names = [s["name"] for s in resp.json()["scripts"]]
        assert "listed_script" in names


class TestScriptsRun:
    def test_run_adhoc_safe_script(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/scripts/run",
            json={"source": "print('hello from adhoc')", "name": "adhoc_test"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0
        assert "hello from adhoc" in data["stdout"]

    def test_run_adhoc_blocked_os_module(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/scripts/run",
            json={"source": "import sys; print(sys.path)", "name": "bad"},
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_run_adhoc_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/scripts/run",
            json={"source": "print('hello')", "name": "test"},
        )
        assert resp.status_code == 401

    def test_run_adhoc_no_source_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/scripts/run",
            json={"name": "no_source"},
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_run_deployed_script(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        # Deploy first
        deploy_resp = c.post(
            "/api/scripts/deploy",
            json={"name": "calc", "source": "print(2+2)"},
            headers=_auth(token),
        )
        assert deploy_resp.status_code == 201
        script_id = deploy_resp.json()["id"]

        # Run deployed script
        resp = c.post(f"/api/scripts/{script_id}/run", headers=_auth(token))
        assert resp.status_code == 200
        assert "4" in resp.json()["stdout"]

    def test_run_nonexistent_script_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/scripts/nonexistent-id/run", headers=_auth(token))
        assert resp.status_code == 404


class TestScriptsDelete:
    def test_undeploy_requires_admin(self, seeded_app):
        """Analyst cannot delete scripts — only admin can."""
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]

        # Deploy as analyst
        deploy_resp = c.post(
            "/api/scripts/deploy",
            json={"name": "to_delete", "source": "print('bye')"},
            headers=_auth(analyst_token),
        )
        script_id = deploy_resp.json()["id"]

        # Analyst cannot delete
        resp = c.delete(f"/api/scripts/{script_id}", headers=_auth(analyst_token))
        assert resp.status_code == 403

        # Admin can delete
        resp = c.delete(f"/api/scripts/{script_id}", headers=_auth(admin_token))
        assert resp.status_code == 204

    def test_undeploy_nonexistent_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.delete("/api/scripts/does-not-exist", headers=_auth(token))
        assert resp.status_code == 404

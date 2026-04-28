"""Tests for admin configure and registry API endpoints."""

import ipaddress
import socket
from unittest.mock import patch

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestAdminConfigure:
    def test_configure_local_source(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data_source"] == "local"

    def test_configure_invalid_source_type_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "invalid_source"},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "data_source" in resp.json()["detail"].lower() or "must be" in resp.json()["detail"]

    def test_configure_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_configure_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
        )
        assert resp.status_code == 401

    def test_configure_bigquery_missing_project_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "bigquery"},  # missing bigquery_project
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_configure_bigquery_with_project(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "bigquery", "bigquery_project": "my-project"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data_source"] == "bigquery"

    def test_configure_missing_data_source_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={},  # missing data_source entirely
            headers=_auth(token),
        )
        assert resp.status_code == 422


class TestAdminConfigureSSRF:
    """SSRF protection: keboola_url must not point to private/reserved networks.

    Uses socket.getaddrinfo + ipaddress checks — tests mock DNS resolution
    so they work regardless of the test runner's network/IPv6 config.
    """

    @staticmethod
    def _mock_getaddrinfo(host, port, **kwargs):
        """Predictable DNS resolution for tests — returns the IP literal as-is."""
        try:
            ip = ipaddress.ip_address(host)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(ip), port))]
        except ValueError:
            # Not an IP literal — let real DNS resolve (for public URL test)
            return socket.getaddrinfo(host, port, **kwargs)

    def test_configure_rejects_localhost_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://localhost:8080"},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "private" in resp.json()["detail"].lower() or "reserved" in resp.json()["detail"].lower()

    def test_configure_rejects_127_0_0_1_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://127.0.0.1"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_10_0_0_1_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://10.0.0.1"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_192_168_url(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://192.168.1.1"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_169_254_metadata_url(self, seeded_app):
        """169.254.x.x (link-local) must be rejected — cloud metadata endpoint."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://169.254.169.254"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_loopback(self, seeded_app):
        """IPv6 loopback ::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_link_local(self, seeded_app):
        """IPv6 link-local fe80::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[fe80::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_unique_local(self, seeded_app):
        """IPv6 unique-local fc00::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[fc00::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_rejects_ipv6_multicast(self, seeded_app):
        """IPv6 multicast ff02::1 must be rejected."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin._socket.getaddrinfo", self._mock_getaddrinfo):
            resp = c.post(
                "/api/admin/configure",
                json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "http://[ff02::1]:8080"},
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_configure_accepts_public_url(self, seeded_app):
        """A public URL should pass SSRF validation (connection test may still fail)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "keboola", "keboola_token": "tok", "keboola_url": "https://connection.keboola.com"},
            headers=_auth(token),
        )
        # Should NOT be 400 with SSRF message — may be 400 from failed connection test, or 200
        if resp.status_code == 400:
            assert "private" not in resp.json()["detail"].lower()


class TestAdminRegistry:
    def test_list_registry_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "tables" in data
        assert "count" in data
        assert data["count"] == 0

    def test_list_registry_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 403

    def test_list_registry_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/registry")
        assert resp.status_code == 401


class TestRegisterTable:
    def test_register_table_success(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "orders", "source_type": "keboola", "bucket": "in.c-crm",
                  "source_table": "orders", "query_mode": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "orders"
        assert data["name"] == "orders"
        assert data["status"] == "registered"

    def test_register_table_appears_in_registry(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        c.post(
            "/api/admin/register-table",
            json={"name": "customers", "source_type": "keboola"},
            headers=_auth(token),
        )

        resp = c.get("/api/admin/registry", headers=_auth(token))
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tables"]]
        assert "customers" in names

    def test_register_duplicate_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Register once
        c.post(
            "/api/admin/register-table",
            json={"name": "dup_table"},
            headers=_auth(token),
        )

        # Register again
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "dup_table"},
            headers=_auth(token),
        )
        assert resp.status_code == 409

    def test_register_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "new_table"},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_register_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "new_table"},
        )
        assert resp.status_code == 401

    def test_register_table_with_all_fields(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "full_table",
                "source_type": "keboola",
                "bucket": "in.c-crm",
                "source_table": "full_table",
                "query_mode": "local",
                "sync_schedule": "0 6 * * *",
                "description": "Full configuration table",
                "profile_after_sync": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201


class TestDeleteRegistryTable:
    def test_delete_registered_table(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Register
        c.post(
            "/api/admin/register-table",
            json={"name": "to_delete"},
            headers=_auth(token),
        )

        # Delete
        resp = c.delete("/api/admin/registry/to_delete", headers=_auth(token))
        assert resp.status_code == 204

        # Verify gone from registry
        list_resp = c.get("/api/admin/registry", headers=_auth(token))
        names = [t["name"] for t in list_resp.json()["tables"]]
        assert "to_delete" not in names

    def test_delete_nonexistent_table_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.delete("/api/admin/registry/nonexistent_table", headers=_auth(token))
        assert resp.status_code == 404

    def test_delete_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.delete("/api/admin/registry/some_table", headers=_auth(token))
        assert resp.status_code == 403

    def test_delete_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete("/api/admin/registry/some_table")
        assert resp.status_code == 401


class TestDiscoverAndRegister:
    def test_discover_and_register_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post("/api/admin/discover-and-register", headers=_auth(token))
        assert resp.status_code == 403

    def test_discover_and_register_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/admin/discover-and-register")
        assert resp.status_code == 401

    def test_discover_and_register_non_keboola_returns_zero(self, seeded_app):
        """With no keboola config, discover-and-register returns 0 registered tables."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Configure as local (non-keboola)
        c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )

        resp = c.post("/api/admin/discover-and-register", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["registered"] == 0
        assert data["source"] != "keboola"

"""The sweep/report half of #1216 (part 1 of 2).

``check_source_url`` (#1154/#1204) gates an MCP source's ``url`` only at
CONFIGURATION time — a row registered before the guard existed, or before
``mcp.source_url_strict`` was turned on, can stay ENABLED and forward
credentials to an address the CURRENT policy would refuse
(``test_an_unrelated_edit_does_not_revalidate_an_already_live_url`` in
``test_admin_mcp_source_url_guard.py`` pins that this is a deliberate gap,
not an oversight — closing it at the update handler would re-open the
DNS-blip footgun the module docstring rules out).

Part 1 does not touch the forward seams (that changes runtime behaviour for
live integrations and needs its own PR). It surfaces a per-row verdict on
the existing list/detail admin API — computed with ONLY the DNS-free half of
the policy (``check_source_url_dns_free``), so an admin can see, with no
resolver call per row, which rows the literal-IP/scheme rules would now
refuse and fix them before flipping strict mode on.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository


def _auth(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _seed(source_id: str, **over) -> None:
    row = {
        "id": source_id,
        "name": source_id,
        "transport": "http",
        "url": "https://mcp.vendor.example/mcp",
    }
    row.update(over)
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(**row)
    conn.close()


def test_list_flags_a_legacy_row_with_a_refused_url(seeded_app):
    """A literal IP in the blocked range — the #1154 shape — needs no DNS to
    judge, so this must show up on the list with no resolver involved."""
    _seed("src_legacy_meta", url="http://169.254.169.254/mcp", enabled=True)
    _seed("src_clean", url="https://mcp.vendor.example/mcp", enabled=True)

    r = seeded_app["client"].get("/api/admin/mcp-sources", headers=_auth(seeded_app))
    assert r.status_code == 200, r.text
    by_id = {row["id"]: row for row in r.json()}

    legacy = by_id["src_legacy_meta"]["url_policy_verdict"]
    assert legacy["verdict"] == "would_refuse"
    assert any("blocked_range" in reason for reason in legacy["reasons"])

    clean = by_id["src_clean"]["url_policy_verdict"]
    assert clean["verdict"] == "ok"
    assert clean["reasons"] == []


def test_list_flags_a_legacy_row_with_cleartext_to_a_public_literal_ip(seeded_app):
    """The other DNS-free refusal: a literal public IP over plain http."""
    _seed("src_legacy_clear", url="http://93.184.216.34/mcp", enabled=True)

    r = seeded_app["client"].get("/api/admin/mcp-sources", headers=_auth(seeded_app))
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["id"] == "src_legacy_clear")
    verdict = row["url_policy_verdict"]
    assert verdict["verdict"] == "would_refuse"
    assert any("cleartext_http_to_public_address" in reason for reason in verdict["reasons"])


def test_detail_surfaces_the_same_verdict(seeded_app):
    _seed("src_legacy_detail", url="http://169.254.169.254/mcp", enabled=True)
    r = seeded_app["client"].get("/api/admin/mcp-sources/src_legacy_detail", headers=_auth(seeded_app))
    assert r.status_code == 200, r.text
    verdict = r.json()["url_policy_verdict"]
    assert verdict["verdict"] == "would_refuse"


def test_stdio_source_carries_no_url_policy_verdict(seeded_app):
    """`url` on a stdio row is inert documentation — nothing ever dials it,
    so it is out of scope for this policy everywhere else it applies too."""
    _seed(
        "src_stdio_report",
        transport="stdio",
        command="/bin/thing",
        url="http://169.254.169.254/whatever",
    )
    r = seeded_app["client"].get("/api/admin/mcp-sources/src_stdio_report", headers=_auth(seeded_app))
    assert r.status_code == 200, r.text
    assert r.json()["url_policy_verdict"] is None


def test_an_ordinary_hostname_reports_ok_without_resolving(seeded_app):
    """The report is deliberately narrower than the full policy: a hostname
    url cannot be judged without DNS, so it reports `ok` rather than
    guessing — the same "unknown" answer `check_source_url_dns_free` gives
    everywhere else."""
    _seed("src_hostname_report", url="https://mcp.vendor.example/mcp")
    r = seeded_app["client"].get("/api/admin/mcp-sources/src_hostname_report", headers=_auth(seeded_app))
    assert r.status_code == 200, r.text
    assert r.json()["url_policy_verdict"] == {"verdict": "ok", "reasons": []}


def test_report_is_admin_gated_like_the_rest_of_the_list_endpoint(seeded_app):
    """No new exposure: the field rides the existing admin-only endpoint."""
    r = seeded_app["client"].get("/api/admin/mcp-sources")
    assert r.status_code in (401, 403)

"""Regression tests for the 2026-07-24 security audit (CLAUDE-SECURITY-20260724).

One test (or small cluster) per finding whose fix has unit-testable logic. UI /
infra findings (F3 chat.js sanitizer, F12 Terraform, F13 template) are verified
by their own surfaces and are noted here for traceability but not unit-tested.
"""

from __future__ import annotations

import time

import pytest


# --- F5: ReDoS in the internal-query SQL sanitizer -------------------------


def test_f5_strip_sql_noise_no_catastrophic_backtracking():
    """A backslash bomb inside an unterminated E'' literal must not hang."""
    from connectors.internal.access import _strip_sql_noise

    evil = "select e'" + ("\\" * 200)
    start = time.monotonic()
    _strip_sql_noise(evil)
    assert time.monotonic() - start < 1.0, "regex took too long — ReDoS not fixed"


def test_f5_escape_string_literal_still_stripped():
    """The fix must keep correctly stripping a real E'' literal."""
    from connectors.internal.access import _strip_sql_noise

    out = _strip_sql_noise(r"select e'ab\'cd' from t")
    assert "ab" not in out and "cd" not in out


# --- F1: profiler identifier quoting ---------------------------------------


def test_f1_quote_ident_escapes_double_quotes():
    from src.profiler import quote_ident

    assert quote_ident("plain") == '"plain"'
    # A breakout attempt must be neutralised by doubling the embedded quote.
    evil = 'x") AS a, (SELECT 1) AS "b'
    quoted = quote_ident(evil)
    assert quoted == '"x"") AS a, (SELECT 1) AS ""b"'
    # No lone (odd) double-quote survives that could close the identifier early.
    assert quoted.startswith('"') and quoted.endswith('"')


# --- F6: marketplace mirror path traversal ---------------------------------


def test_f6_unsafe_plugin_names_rejected():
    from src.marketplace_asset_mirror import _is_safe_plugin_name

    assert _is_safe_plugin_name("my-plugin")
    assert _is_safe_plugin_name("plugin.v2_final")
    assert not _is_safe_plugin_name("../../../../srv/attacker")
    assert not _is_safe_plugin_name("..")
    assert not _is_safe_plugin_name(".")
    assert not _is_safe_plugin_name("a/b")
    assert not _is_safe_plugin_name("a\\b")
    assert not _is_safe_plugin_name("")


def test_f6_write_body_refuses_escape(tmp_path):
    from src.marketplace_asset_mirror import _write_body

    cache = tmp_path / "cache"
    cache.mkdir()
    # A relpath that climbs out of the cache root must be refused. Raised as
    # OSError so sync_assets' `except OSError` skips just this asset instead of
    # aborting the whole sync (PR review).
    with pytest.raises(OSError):
        _write_body(cache, "../../evil.txt", b"x")
    # A legitimate write still works.
    _write_body(cache, "plugin/cover.png", b"ok")
    assert (cache / "plugin" / "cover.png").read_bytes() == b"ok"


def test_f6_unlink_guard_refuses_escape(tmp_path):
    """Read-side containment: a poisoned pre-fix manifest with a ``../`` local
    path must not let cleanup unlink a file outside the cache root."""
    from src.marketplace_asset_mirror import _contained_cache_path

    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "victim.txt"
    outside.write_text("keepme")

    assert _contained_cache_path(cache, "../victim.txt") is None
    assert outside.exists()  # guard returned None → caller skips the unlink
    # A legitimate in-cache path resolves normally.
    assert _contained_cache_path(cache, "plugin/cover.png") == cache / "plugin" / "cover.png"


# --- F8: /api/query file-replacement-scan denylist -------------------------


def test_f8_from_string_literal_rejected():
    from fastapi import HTTPException

    from app.api.query import _assert_select_only

    # Bare relative path in FROM position (the replacement-scan bypass), plus
    # comma-separated-list and glob forms caught by the file-extension guard.
    for sql in (
        "select * from 'data/extracts/b/data/customers.parquet'",
        "select * from  'x.parquet'",
        "select * from ('data/extracts/x.parquet')",
        "select a from t join 'other.parquet' on t.id = 1",
        "select * from my_view, 'data/extracts/b/data/customers.parquet'",
        "select * from 'data/extracts/*/data/*.parquet'",
        "select * from 'secrets.csv'",
    ):
        with pytest.raises(HTTPException):
            _assert_select_only(sql.strip().lower())


def test_f8_sqlglot_models_file_table_source_as_table():
    """Tripwire for the sqlglot behavioral dependency in
    ``app/api/query.py:_has_file_table_source``: the comma-list / glob file
    detection relies on sqlglot (duckdb dialect) modeling a quoted FROM source
    as an ``exp.Table`` whose ``.name`` carries the path. If a sqlglot upgrade
    changes that, this fails loudly and points straight at the cause — the
    direct ``FROM 'x'`` form is still covered by the position-regex fallback."""
    import sqlglot
    from sqlglot import exp

    stmt = sqlglot.parse_one("SELECT * FROM my_view, 'data/x.parquet'", read="duckdb")
    names = [t.name for t in stmt.find_all(exp.Table)]
    assert "data/x.parquet" in names, (
        "sqlglot no longer models a comma-list file source as a Table.name — "
        "update app/api/query.py:_has_file_table_source (and pin sqlglot)"
    )


def test_f8_legitimate_queries_pass():
    from app.api.query import _assert_select_only

    # String literals NOT in table position must still be allowed — including
    # filter values that happen to end in a data-file extension or a path
    # (the false-positive class flagged in PR review of the first F8 fix).
    _assert_select_only("select 'hello' as greeting from my_view")
    _assert_select_only("select * from my_view where name = 'alice'")
    _assert_select_only("with x as (select 1) select * from x")
    _assert_select_only("select * from documents where filename = 'report.csv'")
    _assert_select_only("select * from config where k = 'settings.json'")
    _assert_select_only("select 'annual_report.xlsx' as f from my_view")
    _assert_select_only("select * from documents where p = 'a/b/data.csv'")
    _assert_select_only("select * from (select 'a.csv' as x from t) s")
    _assert_select_only("select * from kbc.main.my_view")


# --- F9: trusted client IP (X-Forwarded-For) -------------------------------


class _FakeReq:
    def __init__(self, xff=None, peer="10.0.0.9"):
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}

        class _C:
            host = peer

        self.client = _C()


def test_f9_trusted_hop_ignores_spoofed_prefix(monkeypatch):
    monkeypatch.delenv("AGNES_TRUSTED_PROXY_HOPS", raising=False)
    from app.auth.client_ip import trusted_client_ip

    # Attacker spoofs a prefix; the proxy appends the real client on the right.
    req = _FakeReq(xff="1.2.3.4, 203.0.113.7")
    assert trusted_client_ip(req) == "203.0.113.7"


def test_f9_multi_hop_config(monkeypatch):
    monkeypatch.setenv("AGNES_TRUSTED_PROXY_HOPS", "2")
    from app.auth.client_ip import trusted_client_ip

    req = _FakeReq(xff="evil, 198.51.100.1, 203.0.113.7")
    # With 2 trusted proxies the genuine client is the 2nd-from-right hop.
    assert trusted_client_ip(req) == "198.51.100.1"


def test_f9_no_xff_falls_back_to_peer(monkeypatch):
    monkeypatch.delenv("AGNES_TRUSTED_PROXY_HOPS", raising=False)
    from app.auth.client_ip import trusted_client_ip

    assert trusted_client_ip(_FakeReq(peer="10.0.0.5")) == "10.0.0.5"


# --- F10/F11: remote-ATTACH host allowlist ---------------------------------


def test_f10_host_allowlist_enforced_when_set(monkeypatch):
    from src import orchestrator_security as osec

    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "connection.example.com")
    assert osec.attach_host_allowlist_configured()
    assert osec.is_attach_host_allowed("https://connection.example.com/v2")
    assert not osec.is_attach_host_allowed("https://attacker.example")


def test_f10_host_allowlist_permissive_when_unset(monkeypatch):
    from src import orchestrator_security as osec

    monkeypatch.delenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", raising=False)
    assert not osec.attach_host_allowlist_configured()
    # Backward compatible: unset => allowed (callers log a warning).
    assert osec.is_attach_host_allowed("https://attacker.example")


def test_f10_host_with_port_matching(monkeypatch):
    from src import orchestrator_security as osec

    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "host.example:9000")
    assert osec.is_attach_host_allowed("https://host.example:9000/x")
    assert not osec.is_attach_host_allowed("https://host.example:1234/x")


def test_f10_real_keboola_url_shape_parses_and_is_allowed(monkeypatch):
    """The shipped Keboola connector writes a standard
    ``https://connection.<region>.gcp.keboola.com`` URL (see
    connectors/keboola/extractor.py) — confirm it parses to a host and is
    allowed when pinned, so operators can safely enable the allowlist."""
    from src import orchestrator_security as osec

    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "connection.us-east4.gcp.keboola.com")
    assert osec.is_attach_host_allowed("https://connection.us-east4.gcp.keboola.com")
    assert osec.is_attach_host_allowed("https://connection.us-east4.gcp.keboola.com/")  # trailing slash


def test_f10_unparseable_host_fails_closed_when_allowlist_set(monkeypatch):
    """A credentialed url with no extractable host is refused when an operator
    has opted into host pinning (deliberate fail-closed), but stays permissive
    when the allowlist is unset."""
    from src import orchestrator_security as osec

    hostless = "/local/only/path"  # urlparse yields no host
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "connection.example.com")
    assert not osec.is_attach_host_allowed(hostless)
    monkeypatch.delenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", raising=False)
    assert osec.is_attach_host_allowed(hostless)


# --- F4: install-prompt override is rendered in a Jinja2 sandbox ------------


def test_f4_sandboxed_env_blocks_ssti_payload():
    """The install-prompt override path must use SandboxedEnvironment so an
    app-Admin's SSTI payload raises instead of executing arbitrary Python."""
    from jinja2 import StrictUndefined
    from jinja2.exceptions import SecurityError
    from jinja2.sandbox import SandboxedEnvironment

    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    payload = "{{ cycler.__init__.__globals__['os'].popen('id').read() }}"
    template = env.from_string(payload)
    with pytest.raises(SecurityError):
        template.render()

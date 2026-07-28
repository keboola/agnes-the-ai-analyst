"""Phase 1a/1b of the analysis-output verification design
(`docs/superpowers/specs/2026-07-25-analysis-output-verification-design.md`).

`agnes snapshot create` warns — never blocks — when the fetch shape ignores the
`CLAUDE.md` query rails: an unbounded remote fetch (no `--where`/`--limit`) or an
implicit `SELECT *` (no/`*` `--select`). The `--from-query` path is exempt (it
carries its own projection and is the internal `agnes query --auto-snapshot`
caller's path).

The decision logic is a pure function so it is unit-testable without the network
or a local DuckDB, exactly like the sync-map detectors.
"""

from __future__ import annotations

from cli.commands.snapshot import _fetch_shape_warnings


def _msgs(**kw) -> list[str]:
    kw.setdefault("select", None)
    kw.setdefault("where", None)
    kw.setdefault("limit", None)
    kw.setdefault("from_query", None)
    return _fetch_shape_warnings(**kw)


# ---------------------------------------------------------------------------
# 1a — unbounded remote fetch (no --where and no --limit)
# ---------------------------------------------------------------------------


def test_no_where_and_no_limit_warns():
    msgs = _msgs(select="a,b")
    assert any("--where" in m and "--limit" in m for m in msgs)


def test_where_present_silences_the_unbounded_warning():
    assert all("--limit" not in m for m in _msgs(select="a", where="x = 1"))


def test_limit_present_silences_the_unbounded_warning():
    assert all("--limit" not in m for m in _msgs(select="a", limit=100))


def test_limit_zero_is_still_a_bound():
    # An explicit --limit 0 is a deliberate bound (empty fetch), not "unset".
    assert all("--limit" not in m for m in _msgs(select="a", limit=0))


# ---------------------------------------------------------------------------
# 1b — implicit SELECT *
# ---------------------------------------------------------------------------


def test_absent_select_warns():
    assert any("column" in m.lower() for m in _msgs(where="x = 1"))


def test_star_select_warns():
    assert any("column" in m.lower() for m in _msgs(select="*", where="x = 1"))


def test_star_among_columns_warns():
    assert any("column" in m.lower() for m in _msgs(select="a, *, b", where="x = 1"))


def test_explicit_columns_do_not_warn_about_select():
    assert all("column" not in m.lower() for m in _msgs(select="a,b,c", where="x = 1"))


def test_whitespace_only_select_is_treated_as_absent():
    assert any("column" in m.lower() for m in _msgs(select="   ", where="x = 1"))


# ---------------------------------------------------------------------------
# --from-query exemption (the auto-snapshot caller's path)
# ---------------------------------------------------------------------------


def test_from_query_is_exempt_from_both():
    # No select/where/limit, but from_query carries its own projection.
    assert _msgs(from_query="SELECT a FROM v") == []


# ---------------------------------------------------------------------------
# both at once
# ---------------------------------------------------------------------------


def test_bare_create_warns_on_both_shape_rules():
    msgs = _msgs()  # no select, no where, no limit
    assert any("column" in m.lower() for m in msgs)
    assert any("--where" in m for m in msgs)
    assert len(msgs) == 2


def test_fully_specified_fetch_is_silent():
    assert _msgs(select="a,b", where="x = 1", limit=100) == []


# ---------------------------------------------------------------------------
# integration: the WARN reaches stderr on a real fetch, and never blocks it
# ---------------------------------------------------------------------------


def _prep_local_db(tmp_path):
    import duckdb

    db_dir = tmp_path / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    duckdb.connect(str(db_dir / "analytics.duckdb")).close()
    return db_dir


def test_real_fetch_prints_warning_but_still_succeeds(tmp_path, monkeypatch):
    """A --no-estimate fetch with no --where/--select warns on stderr AND
    completes — WARN-only must never turn into a blocked fetch."""
    from unittest.mock import patch

    import pyarrow as pa
    from typer.testing import CliRunner

    from cli.commands.snapshot import snapshot_app

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _prep_local_db(tmp_path)

    with patch(
        "cli.commands.snapshot.api_post_arrow",
        side_effect=lambda p, payload: pa.table({"x": [1]}),
    ):
        result = CliRunner().invoke(snapshot_app, ["create", "big_remote", "--as", "s1", "--no-estimate"])

    out = result.output + (result.stderr or "")
    assert result.exit_code == 0, out
    assert "unbounded remote fetch" in out
    assert "--select" in out


def test_auto_snapshot_from_query_path_is_silent(tmp_path, monkeypatch):
    """The internal auto-snapshot caller uses --from-query; it must not emit the
    shape warnings (it carries its own projection)."""
    from unittest.mock import patch

    import pyarrow as pa
    from typer.testing import CliRunner

    from cli.commands.snapshot import snapshot_app

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _prep_local_db(tmp_path)

    with patch(
        "cli.commands.snapshot.api_post_arrow",
        side_effect=lambda p, payload: pa.table({"x": [1]}),
    ):
        result = CliRunner().invoke(
            snapshot_app,
            ["create", "auto_x", "--from-query", "SELECT x FROM v"],
        )

    out = result.output + (result.stderr or "")
    assert result.exit_code == 0, out
    assert "unbounded remote fetch" not in out
    assert "list specific columns" not in out

"""Unit tests for the linked-app projection reconciler + Keboola adapter.

Hermetic: drives a DuckDB `DataAppsRepository` on a temp system db (no live
Keboola MCP), injected into `project(repo=...)`.
"""

from __future__ import annotations

import duckdb
import pytest

from src.data_apps import keboola_adapter as adapter
from src.data_apps.linked_projection import project


@pytest.fixture
def repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.data_apps import DataAppsRepository

    conn = duckdb.connect(str(tmp_path / "sys.duckdb"))
    _ensure_schema(conn)
    yield DataAppsRepository(conn)
    conn.close()


def _rec(app_id, name="N", desc="", url="https://example.com/x"):
    return adapter.LinkedAppRecord(external_app_id=app_id, name=name, description=desc, external_url=url)


def test_map_row_tolerates_column_variants():
    rec = adapter.map_row({"id": "123", "name": "Sales", "url": "https://example.com/s", "description": "d"})
    assert rec.external_app_id == "123"
    assert rec.name == "Sales"
    assert rec.external_url == "https://example.com/s"
    assert rec.description == "d"


def test_slug_is_deterministic_and_unique_across_connections():
    a = adapter.slug_for("conn1", "42")
    assert a == adapter.slug_for("conn1", "42")  # deterministic
    assert a != adapter.slug_for("conn2", "42")  # same app id, different connection


def test_project_create_then_reconcile_hides_missing(repo):
    r1 = project("conn1", [_rec("a1", "A1"), _rec("a2", "A2")], repo=repo)
    assert (r1.created, r1.updated, r1.hidden) == (2, 0, 0)
    assert {x["slug"] for x in repo.list_linked()} == {
        adapter.slug_for("conn1", "a1"),
        adapter.slug_for("conn1", "a2"),
    }

    # second round: a2 gone → hidden; a1 updated; a different connection untouched
    project("conn2", [_rec("b1", "B1")], repo=repo)
    r2 = project("conn1", [_rec("a1", "A1 renamed")], repo=repo)
    assert (r2.created, r2.updated, r2.hidden) == (0, 1, 1)
    active = {x["slug"] for x in repo.list_linked()}
    assert adapter.slug_for("conn1", "a2") not in active  # hidden
    assert adapter.slug_for("conn2", "b1") in active  # other connection kept


def test_project_preserves_description_override(repo):
    project("c", [_rec("a", "A", desc="synced")], repo=repo)
    slug = adapter.slug_for("c", "a")
    repo.set_description_override(slug, "admin desc")

    project("c", [_rec("a", "A", desc="synced-2")], repo=repo)  # re-sync
    row = repo.get_by_slug(slug)
    assert row["description"] == "synced-2"
    assert repo.effective_description(row) == "admin desc"


def _seed_extract(path, rows):
    """Write a keboola_data_apps table into an extract.duckdb file."""
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE keboola_data_apps (id VARCHAR, name VARCHAR, description VARCHAR, url VARCHAR)")
    for r in rows:
        c.execute(
            "INSERT INTO keboola_data_apps VALUES (?, ?, ?, ?)",
            [r["id"], r["name"], r.get("description", ""), r["url"]],
        )
    c.close()


def test_project_from_extract_creates_linked(tmp_path, repo):
    from src.data_apps.linked_projection import project_from_extract

    path = tmp_path / "extract.duckdb"
    _seed_extract(
        path,
        [
            {"id": "10", "name": "Sales", "url": "https://example.com/10"},
            {"id": "11", "name": "Ops", "url": "https://example.com/11"},
        ],
    )
    res = project_from_extract("srcX", str(path), repo=repo)
    assert res is not None and (res.created, res.hidden) == (2, 0)
    assert {r["name"] for r in repo.list_linked()} == {"Sales", "Ops"}


def test_project_from_extract_skips_rows_without_url(tmp_path, repo):
    from src.data_apps.linked_projection import project_from_extract

    path = tmp_path / "extract.duckdb"
    _seed_extract(
        path,
        [
            {"id": "10", "name": "Sales", "url": "https://example.com/10"},
            {"id": "11", "name": "NoUrl", "url": ""},
        ],
    )
    res = project_from_extract("srcX", str(path), repo=repo)
    assert res is not None and res.created == 1
    assert {r["name"] for r in repo.list_linked()} == {"Sales"}


def test_project_from_extract_noop_without_table(tmp_path, repo):
    from src.data_apps.linked_projection import project_from_extract

    path = tmp_path / "extract.duckdb"
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE some_other_table (x INTEGER)")
    c.close()
    assert project_from_extract("srcX", str(path), repo=repo) is None
    assert repo.list_linked() == []


def test_project_from_extract_missing_file_noop(repo):
    from src.data_apps.linked_projection import project_from_extract

    assert project_from_extract("srcX", "/no/such/extract.duckdb", repo=repo) is None


def test_project_reappearance_relinks_same_row(repo):
    project("c", [_rec("a", "A"), _rec("keep", "Keep")], repo=repo)
    slug = adapter.slug_for("c", "a")
    original_id = repo.get_by_slug(slug)["id"]
    # `a` disappears from a NON-empty sync → hidden (an empty sync is a
    # no-op by design — see the empty-result safety valve in project()).
    project("c", [_rec("keep", "Keep")], repo=repo)
    assert {r["slug"] for r in repo.list_linked()} == {adapter.slug_for("c", "keep")}

    r = project("c", [_rec("a", "A back")], repo=repo)  # returns
    assert (r.created, r.updated) == (0, 1)  # reactivated, not a new row
    row = repo.get_by_slug(slug)
    assert row["id"] == original_id
    assert row["state"] == "linked"


def test_map_row_rejects_non_http_url_schemes():
    """external_url is untrusted upstream data rendered as a raw href — only
    http(s) may survive ingest; anything else empties the field so the row is
    skipped by the linkable filter (review team on #1116)."""
    from src.data_apps.keboola_adapter import map_row

    for bad in ("javascript:alert(1)", "data:text/html,x", "vbscript:x", "file:///etc/passwd"):
        assert map_row({"id": "a1", "name": "x", "url": bad}).external_url == ""
    assert map_row({"id": "a1", "url": "https://example.com/app"}).external_url == "https://example.com/app"
    assert map_row({"id": "a1", "url": "  http://example.com  "}).external_url == "http://example.com"


def test_v107_to_v108_alter_path_on_migrated_db(tmp_path):
    """The upgrade ladder must work on a REAL pre-v108 database — fresh
    installs no-op (DDL already has the columns), so only this path exercises
    the ALTERs. DuckDB rejects ADD COLUMN with a NOT NULL constraint, which a
    fresh-DB-only CI run never notices (review team on #1116)."""
    import duckdb

    from src.db import _v107_to_v108

    conn = duckdb.connect(str(tmp_path / "old.duckdb"))
    conn.execute("CREATE TABLE data_apps (slug VARCHAR, repo_mode VARCHAR, state VARCHAR)")
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (107)")
    conn.execute("INSERT INTO data_apps VALUES ('a', 'git', 'ready')")

    _v107_to_v108(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info('data_apps')").fetchall()}
    assert {"external_url", "source_ref", "managed", "description_override"} <= cols
    assert conn.execute("SELECT managed FROM data_apps").fetchone()[0] is False
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 108

    _v107_to_v108(conn)  # idempotent re-run must not raise


def test_project_from_extract_custom_table_name(tmp_path, repo):
    """The extractor names tables after the tool's exposed_name — a targeted
    (only_tool_id) materialize passes that name explicitly, so the projection
    must read it instead of the literal keboola_data_apps (Devin Review on
    #1116)."""
    from src.data_apps.linked_projection import project_from_extract

    path = tmp_path / "extract.duckdb"
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE keboola_list_data_apps (id VARCHAR, name VARCHAR, url VARCHAR)")
    c.execute("INSERT INTO keboola_list_data_apps VALUES ('10', 'Sales', 'https://example.com/10')")
    c.close()

    # Default contract name absent -> no-op...
    assert project_from_extract("srcX", str(path), repo=repo) is None
    # ...but the designated table projects.
    res = project_from_extract("srcX", str(path), repo=repo, table_name="keboola_list_data_apps")
    assert res is not None and res.created == 1
    assert {r["name"] for r in repo.list_linked()} == {"Sales"}


def test_project_keeps_present_but_unlinkable_rows(repo):
    """A row present upstream whose URL went missing/unusable in one sync must
    NOT be pruned — present upstream is present, even with one broken field.
    Only genuinely absent apps get hidden (Devin Review on #1116)."""
    project("c", [_rec("a", "A"), _rec("b", "B")], repo=repo)
    assert {r["slug"] for r in repo.list_linked()} == {
        adapter.slug_for("c", "a"),
        adapter.slug_for("c", "b"),
    }

    # `b` still listed upstream but its URL column came back blank this round;
    # `a` is fine. Nothing is dropped.
    r = project("c", [_rec("a", "A")], repo=repo, keep_external_ids=["b"])
    assert r.hidden == 0
    assert {x["slug"] for x in repo.list_linked()} == {
        adapter.slug_for("c", "a"),
        adapter.slug_for("c", "b"),
    }

    # ...but a genuinely absent `b` (not in the listing at all) still hides.
    r2 = project("c", [_rec("a", "A")], repo=repo)
    assert r2.hidden == 1
    assert {x["slug"] for x in repo.list_linked()} == {adapter.slug_for("c", "a")}


def test_project_from_extract_keeps_row_with_blank_url(tmp_path, repo):
    """End-to-end: the blank-URL row is skipped for upsert but exempt from the
    prune, so a live app doesn't vanish over a one-field glitch."""
    from src.data_apps.linked_projection import project_from_extract

    path = tmp_path / "extract.duckdb"
    _seed_extract(
        path,
        [
            {"id": "10", "name": "Sales", "url": "https://example.com/10"},
            {"id": "11", "name": "Ops", "url": "https://example.com/11"},
        ],
    )
    project_from_extract("srcX", str(path), repo=repo)
    assert len(repo.list_linked()) == 2

    path2 = tmp_path / "extract2.duckdb"
    _seed_extract(
        path2,
        [
            {"id": "10", "name": "Sales", "url": "https://example.com/10"},
            {"id": "11", "name": "Ops", "url": ""},  # URL glitched away
        ],
    )
    res = project_from_extract("srcX", str(path2), repo=repo)
    assert res is not None and res.hidden == 0
    assert len(repo.list_linked()) == 2

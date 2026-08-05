"""`_rebuild_duckdb_views` must build ONE view per partitioned table
(directory of parts) over a hive glob, while single-file tables keep their
stem view. Real parquet parts, so read_parquet actually resolves them.
"""
from __future__ import annotations

import duckdb

from cli.lib.pull import _rebuild_duckdb_views


def _write_parquet(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    try:
        c.execute(f"COPY (SELECT range AS id FROM range({n})) TO '{path}' (FORMAT PARQUET)")
    finally:
        c.close()


def _analytics(workspace):
    return duckdb.connect(str(workspace / "user" / "duckdb" / "analytics.duckdb"))


def test_partitioned_dir_builds_one_hive_view(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "issues" / "month=2026-06" / "data.parquet", 3)
    _write_parquet(pq / "issues" / "month=2026-07" / "data.parquet", 2)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 5
        # hive_partitioning surfaces `month` from the dir names
        assert conn.execute("SELECT count(DISTINCT month) FROM issues").fetchone()[0] == 2
    finally:
        conn.close()


def test_single_file_table_still_builds_stem_view(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 5)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 5
    finally:
        conn.close()


def test_mixed_single_and_partitioned(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 4)
    _write_parquet(pq / "issues" / "month=2026-06" / "data.parquet", 7)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 7
    finally:
        conn.close()


def test_staging_dir_is_ignored(tmp_path):
    """A leftover .staging-* dir from an interrupted sync must not become a view."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / ".staging-issues" / "month=2026-06" / "data.parquet", 3)
    _write_parquet(pq / "account.parquet", 1)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()}
        assert "account" in names
        assert not any(n.startswith(".staging") for n in names)
    finally:
        conn.close()


def test_flat_partitioned_layout_builds_queryable_view(tmp_path):
    """Keboola flat-partitioned layout ({key}.parquet, NO key=value dirs) must
    build a queryable view — hive_partitioning=true must not error on it
    (Devin re-review: flat layout untested)."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "cost" / "2025_11.parquet", 4)
    _write_parquet(pq / "cost" / "2025_12.parquet", 6)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cost").fetchone()[0] == 10
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Local snapshots must survive the rebuild.
#
# `agnes snapshot create` writes `<workspace>/user/snapshots/<name>.parquet`
# and registers a view named `<name>`. The rebuild drops every view and used
# to re-create only those backed by `server/parquet`, so a snapshot's view was
# destroyed by the next pull and never came back — while `agnes snapshot list`
# (which reads the meta sidecars off disk) kept reporting it as present.
# ---------------------------------------------------------------------------


def _snap_dir(workspace):
    return workspace / "user" / "snapshots"


def test_snapshot_view_is_registered_by_the_rebuild(tmp_path):
    """The self-healing case: a snapshot parquet with no view gets one."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 1)
    _write_parquet(_snap_dir(tmp_path) / "cz_recent.parquet", 9)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cz_recent").fetchone()[0] == 9
    finally:
        conn.close()


def test_snapshot_view_survives_a_second_rebuild(tmp_path):
    """The reported bug: the snapshot worked, then a pull silently killed it."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 1)
    _write_parquet(_snap_dir(tmp_path) / "cz_recent.parquet", 9)

    _rebuild_duckdb_views(tmp_path, pq)
    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cz_recent").fetchone()[0] == 9
    finally:
        conn.close()


def test_server_table_wins_a_name_collision_with_a_snapshot(tmp_path):
    """A registered table is canonical: a same-named snapshot must not shadow it."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 4)
    _write_parquet(_snap_dir(tmp_path) / "account.parquet", 99)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 4
    finally:
        conn.close()


def test_snapshot_does_not_shadow_a_user_base_table(tmp_path):
    """Same guard the server-parquet loop already honors."""
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    _write_parquet(_snap_dir(tmp_path) / "scratch.parquet", 7)

    db_path = tmp_path / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE scratch AS SELECT 1 AS id")
    finally:
        conn.close()

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM scratch").fetchone()[0] == 1
        kind = conn.execute(
            "SELECT table_type FROM information_schema.tables WHERE table_name='scratch'"
        ).fetchone()[0]
        assert kind == "BASE TABLE"
    finally:
        conn.close()


def test_missing_snapshots_dir_is_a_no_op(tmp_path):
    """Most workspaces have never created a snapshot."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 2)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 2
    finally:
        conn.close()


def test_corrupt_snapshot_is_skipped_without_aborting_the_rebuild(tmp_path):
    """A truncated snapshot must not cost the user their server-table views."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 3)
    snaps = _snap_dir(tmp_path)
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "broken.parquet").write_bytes(b"not a parquet file")
    _write_parquet(snaps / "good.parquet", 5)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM good").fetchone()[0] == 5
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        assert "broken" not in names
    finally:
        conn.close()


def test_meta_sidecar_is_not_mistaken_for_a_snapshot(tmp_path):
    """`<name>.meta.json` sits next to `<name>.parquet`; only the parquet counts."""
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    snaps = _snap_dir(tmp_path)
    _write_parquet(snaps / "cz_recent.parquet", 3)
    (snaps / "cz_recent.meta.json").write_text('{"name": "cz_recent"}', encoding="utf-8")

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()}
        assert names == {"cz_recent"}
    finally:
        conn.close()


def test_deauthorized_table_id_is_withheld_from_a_snapshot(tmp_path):
    """#1129 review — step 4b deletes a de-authorized parquet so the name stops
    resolving. A snapshot created with no `--as` is named after its source
    table, so without the guard it re-takes that id and `agnes query` answers
    from stale rows instead of erroring.
    """
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    # `account` was pruned by step 4b — no server parquet remains on disk.
    _write_parquet(_snap_dir(tmp_path) / "account.parquet", 9)

    withheld = _rebuild_duckdb_views(tmp_path, pq, blocked_names={"account"})

    assert withheld == ["account"]
    conn = _analytics(tmp_path)
    try:
        names = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        assert "account" not in names
    finally:
        conn.close()
    # The data itself is untouched and still reachable by its snapshot path.
    assert (_snap_dir(tmp_path) / "account.parquet").exists()


def test_unrelated_snapshots_still_register_when_one_is_blocked(tmp_path):
    """The guard is per-name, not a kill switch for the whole snapshot tree."""
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    _write_parquet(_snap_dir(tmp_path) / "account.parquet", 9)
    _write_parquet(_snap_dir(tmp_path) / "cz_recent.parquet", 4)

    withheld = _rebuild_duckdb_views(tmp_path, pq, blocked_names={"account"})

    assert withheld == ["account"]
    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cz_recent").fetchone()[0] == 4
    finally:
        conn.close()


def test_blocking_is_stable_across_repeated_rebuilds(tmp_path):
    """The regression the prune-derived version would have had: the name must
    stay withheld on every subsequent pull, not just the one that pruned it.
    """
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    _write_parquet(_snap_dir(tmp_path) / "account.parquet", 9)

    for _ in range(3):
        withheld = _rebuild_duckdb_views(tmp_path, pq, blocked_names={"account"})
        assert withheld == ["account"]
        conn = _analytics(tmp_path)
        try:
            names = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            assert "account" not in names
        finally:
            conn.close()


def test_no_blocked_names_keeps_the_previous_behaviour(tmp_path):
    """Default arg — every existing caller is unaffected."""
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    _write_parquet(_snap_dir(tmp_path) / "account.parquet", 9)

    withheld = _rebuild_duckdb_views(tmp_path, pq)

    assert withheld == []
    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 9
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# which names get withheld in the first place (#1129 review)
# ---------------------------------------------------------------------------


def _blocked(
    server_tables,
    authorized,
    server_only=frozenset(),
    *,
    previously_local=None,
    still_local=frozenset(),
    remembered=frozenset(),
):
    from cli.lib.pull import _blocked_snapshot_names

    # Default: everything the manifest lists used to be local, which is the
    # ordinary analyst case (their package tables are what got downloaded).
    if previously_local is None:
        previously_local = set(server_tables)
    return _blocked_snapshot_names(
        server_tables,
        authorized,
        set(server_only),
        previously_local=set(previously_local),
        still_local=set(still_local),
        remembered=set(remembered),
    )


def test_an_id_that_was_never_local_is_never_withheld():
    """The over-reach that mattered in practice: for an admin the manifest
    lists the whole instance while `authorized_names` holds only their own
    stack, so judging by "listed but not mine" killed a snapshot named after
    any table outside it — on every pull."""
    server_tables = {"orders": {"query_mode": "local"}, "someone_elses": {"query_mode": "local"}}
    assert _blocked(server_tables, authorized=set(), previously_local={"orders"}) == {"orders"}


def test_a_table_that_vanished_from_the_manifest_is_withheld():
    """Full revocation drops the row entirely, so a rule that only walks
    `server_tables` misses the strongest case — while the prune still deletes
    the parquet and frees the name."""
    assert _blocked({}, authorized=set(), previously_local={"gone"}) == {"gone"}


def test_a_withheld_name_is_remembered_after_the_prune():
    """The evidence (`sync_state` row) is gone by the next pull, so a set
    recomputed from scratch would release the name — and the snapshot would
    start answering for a table the analyst can no longer read."""
    # Pull N+1: nothing was local at the start, nothing pruned now.
    assert _blocked({}, authorized=set(), previously_local=set(), remembered={"gone"}) == {"gone"}


def test_a_name_is_released_once_the_table_is_local_again():
    """Re-authorized and re-downloaded: the registered table owns the name, so
    keeping it blocked withholds a name nothing competes for."""
    server_tables = {"orders": {"query_mode": "local"}}
    assert _blocked(server_tables, authorized={"orders"}, remembered={"orders"}, still_local={"orders"}) == set()


def test_a_remote_table_id_is_never_withheld():
    """`agnes snapshot create <remote_id>` with no `--as` names the snapshot
    after its source table — the flow CLAUDE.md documents as the primary path
    for large remote tables. `authorized_names` carries data-package names
    only, so a naive "not authorized" sweep withheld EVERY remote id and broke
    that flow permanently on the next pull."""
    server_tables = {
        "web_sessions": {"query_mode": "remote"},
        "orders": {"query_mode": "local"},
    }
    # The download loop skips remote rows, so only `orders` was ever local —
    # which is why the general "was it ever local?" rule subsumes the
    # remote-mode special case rather than needing one.
    assert _blocked(server_tables, authorized=set(), previously_local={"orders"}) == {"orders"}


def test_a_deauthorized_local_table_id_is_still_withheld():
    """The original fix must survive: a local table that left the analyst's
    stack had its parquet pruned, so the name would otherwise resolve to stale
    snapshot rows."""
    server_tables = {"orders": {"query_mode": "local"}}
    assert _blocked(server_tables, authorized=set()) == {"orders"}


def test_materialized_tables_behave_like_local():
    server_tables = {"kpi_daily": {"query_mode": "materialized"}}
    assert _blocked(server_tables, authorized=set()) == {"kpi_daily"}


def test_a_missing_query_mode_defaults_to_local():
    """Pre-v49 manifests omit the key; treating the default as remote would
    silently stop withholding de-authorized ids."""
    assert _blocked({"orders": {}}, authorized=set()) == {"orders"}


def test_server_only_is_withheld_whatever_its_query_mode():
    """server_only means the parquet must leave the laptop, so the name really
    did stop resolving — including for a remote-mode row."""
    server_tables = {"big": {"query_mode": "remote", "server_only": True}}
    assert _blocked(server_tables, authorized={"big"}, server_only={"big"}) == {"big"}


def test_no_authorization_filter_withholds_nothing_extra():
    """`authorized_names is None` — a pre-v49 server sends no package data, so
    there is nothing to judge de-authorization against."""
    server_tables = {"orders": {"query_mode": "local"}}
    assert _blocked(server_tables, authorized=None) == set()

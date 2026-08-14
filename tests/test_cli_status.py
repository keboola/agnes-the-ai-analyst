"""Tests for agnes status (workspace status)."""

import json

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


from cli.commands.status import status_app

runner = CliRunner()


def test_status_uninitialized_workspace(tmp_path, monkeypatch):
    """Empty folder → exit 0, output indicates uninitialized state."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(status_app)
    assert result.exit_code in (0, 1)
    out = result.output.lower()
    assert "no" in out  # "Initialized: no" or similar
    assert "agnes init" in _clean(result.output)  # hint to initialize


def test_status_initialized_via_legacy_claude_md_marker(tmp_path, monkeypatch):
    """Pre-#259 workspaces have only the legacy `# AI Data Analyst` string
    in CLAUDE.md (no `.claude/init-complete` sentinel) — keep recognising
    them so older analyst checkouts don't flip to 'Initialized: no' after
    a CLI upgrade."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "duckdb" / "analytics.duckdb").touch()
    (tmp_path / "server" / "parquet").mkdir(parents=True)
    (tmp_path / "server" / "parquet" / "tbl1.parquet").touch()

    result = runner.invoke(status_app)
    assert result.exit_code == 0
    out = result.output.lower()
    assert "yes" in out
    assert "1" in _clean(result.output)


def test_status_initialized_via_init_complete_sentinel(tmp_path, monkeypatch):
    """Override-mode workspaces (customer-supplied Initial-Workspace
    templates whose CLAUDE.md body legitimately omits the literal
    'AI Data Analyst' substring) must still report 'Initialized: yes'
    when `.claude/init-complete` exists. The sentinel is authoritative
    for override mode; the legacy CLAUDE.md grep is fallback-only."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    # Heading deliberately omits the canonical "AI Data Analyst" marker;
    # this is what a custom override template can look like in the wild.
    (tmp_path / "CLAUDE.md").write_text("# Acme — Custom Workspace\n")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text(
        "completed_at: 2026-05-26T11:33:30Z\nagnes_version: 0.55.13\noverride: true\n"
    )
    result = runner.invoke(status_app)
    assert result.exit_code == 0
    assert "yes" in result.output.lower()
    assert "agnes init" not in _clean(result.output)


def test_status_json(tmp_path, monkeypatch):
    """--json flag returns machine-readable output."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("agnes_version: 0.55.13\n")
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "workspace" in body and "initialized" in body
    assert body["initialized"] is True


# ---------------------------------------------------------------------------
# Table counting — the counter reported 0 for populated workspaces because it
# globbed `server/parquet/*.parquet` non-recursively and ignored the v49
# `.claude/data/` tree entirely.
# ---------------------------------------------------------------------------


def _init(workspace):
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / ".claude" / "init-complete").write_text("agnes_version: test\n")


def _count(workspace, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["parquet_tables"]


def test_partitioned_table_counts_once_not_zero(tmp_path, monkeypatch):
    """A partitioned table is a DIRECTORY of parts. The old non-recursive
    glob returned 0 for it however many parts were on disk."""
    _init(tmp_path)
    parts = tmp_path / "server" / "parquet" / "jira_issues"
    (parts / "month=2026-01").mkdir(parents=True)
    (parts / "month=2026-02").mkdir(parents=True)
    (parts / "month=2026-01" / "data.parquet").touch()
    (parts / "month=2026-02" / "data.parquet").touch()

    assert _count(tmp_path, monkeypatch) == 1


def test_shared_stack_sync_tree_is_counted(tmp_path, monkeypatch):
    """Tables delivered by the v49 stack sync live under
    `.claude/data/_shared/` and were previously invisible to the count."""
    _init(tmp_path)
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "orders.parquet").touch()
    (shared / "customers.parquet").touch()

    assert _count(tmp_path, monkeypatch) == 2


def test_package_reference_links_do_not_double_count(tmp_path, monkeypatch):
    """`_direct/` and `<package>/` entries are references INTO `_shared`, so a
    table shipped by two packages must still count once."""
    _init(tmp_path)
    data = tmp_path / ".claude" / "data"
    (data / "_shared").mkdir(parents=True)
    (data / "_shared" / "orders.parquet").touch()
    for ref_dir in ("_direct", "pkg_a", "pkg_b"):
        (data / ref_dir).mkdir(parents=True)
        (data / ref_dir / "orders.parquet").touch()

    assert _count(tmp_path, monkeypatch) == 1


def test_same_table_in_both_trees_counts_once(tmp_path, monkeypatch):
    """Both pull phases can land the same table id; it is one table."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "orders.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "orders.parquet").touch()

    assert _count(tmp_path, monkeypatch) == 1


def test_non_slug_name_in_both_trees_counts_once(tmp_path, monkeypatch):
    """The two trees are keyed differently: the legacy tree's stem is the
    flat manifest key (`sync_state.table_id` == `table_registry.name`), while
    `_shared/` uses `table_registry.id`, which registration derives by
    slugifying the name. A table named `Agnes Audit Log` therefore lands as
    two different stems and was counted twice, inflating the total for any
    workspace whose names carry spaces or capitals."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "Agnes Audit Log.parquet").touch()  # stem = registry name
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "agnes_audit_log.parquet").touch()  # stem = registry id

    assert _count(tmp_path, monkeypatch) == 1


def test_non_slug_partitioned_dir_matches_shared_stem(tmp_path, monkeypatch):
    """Same keying mismatch, partitioned layout: the legacy side is a
    directory named after the registry name."""
    _init(tmp_path)
    parts = tmp_path / "server" / "parquet" / "Jira Issues"
    (parts / "month=2026-01").mkdir(parents=True)
    (parts / "month=2026-01" / "data.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "jira_issues.parquet").touch()

    assert _count(tmp_path, monkeypatch) == 1


def test_staging_and_empty_dirs_are_not_counted(tmp_path, monkeypatch):
    """An interrupted partitioned sync leaves a `.staging-<tid>` scratch dir
    and can leave an empty `<tid>/`; neither is queryable."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    (legacy / ".staging-orders").mkdir(parents=True)
    (legacy / ".staging-orders" / "data.parquet").touch()
    (legacy / "abandoned").mkdir(parents=True)
    (legacy / "real.parquet").touch()

    assert _count(tmp_path, monkeypatch) == 1


def test_populated_but_uninitialized_workspace_explains_itself(tmp_path, monkeypatch):
    """`agnes pull` only needs a workspace-SHAPED dir, so data can be present
    with no init sentinel. Reporting a bare 'no' beside a populated table
    count reads as a contradiction — name the half that is missing."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "orders.parquet").touch()

    result = runner.invoke(status_app)
    out = _clean(result.output)
    assert "Tables    : 1" in out
    assert "Initialized: no" in out
    assert "holds data" in out and "never ran here" in out

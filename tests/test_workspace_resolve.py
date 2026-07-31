"""Resolver matrix for cli/lib/workspace_resolve.py (spec §5.1).

Precedence: AGNES_LOCAL_DIR env (always wins, even unshaped) →
cwd-if-shaped → workspace_root-if-shaped → None. A stale anchor
(deleted dir / unshaped) degrades to None, never to a bogus path.
"""

from pathlib import Path

import pytest

from cli.lib.workspace_resolve import is_workspace_shaped, resolve_data_workspace


def _make_shaped(p: Path, marker: str = "sentinel") -> Path:
    p.mkdir(parents=True, exist_ok=True)
    if marker == "sentinel":
        (p / ".claude").mkdir(parents=True, exist_ok=True)
        (p / ".claude" / "init-complete").write_text("x", encoding="utf-8")
    elif marker == "duckdb":
        (p / "user" / "duckdb").mkdir(parents=True, exist_ok=True)
        (p / "user" / "duckdb" / "analytics.duckdb").write_bytes(b"")
    elif marker == "parquet":
        (p / "server" / "parquet").mkdir(parents=True, exist_ok=True)
    return p


@pytest.mark.parametrize("marker", ["sentinel", "duckdb", "parquet"])
def test_is_workspace_shaped_markers(tmp_path, marker):
    assert is_workspace_shaped(_make_shaped(tmp_path / "ws", marker)) is True


def test_is_workspace_shaped_negative(tmp_path):
    plain = tmp_path / "repo"
    plain.mkdir()
    assert is_workspace_shaped(plain) is False


def test_env_wins_even_when_unshaped(tmp_path, monkeypatch):
    target = tmp_path / "env-target"
    target.mkdir()
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(target))
    monkeypatch.chdir(_make_shaped(tmp_path / "cwd-ws"))
    assert resolve_data_workspace() == target.resolve()


def test_cwd_shaped_beats_anchor(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    cwd = _make_shaped(tmp_path / "cwd-ws")
    monkeypatch.chdir(cwd)
    assert resolve_data_workspace() == cwd.resolve()


def test_anchor_used_when_cwd_unshaped(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() == anchor.resolve()


def test_stale_anchor_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(
        "cli.lib.workspace_resolve.get_workspace_root",
        lambda: str(tmp_path / "deleted-anchor"),
    )
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() is None


def test_no_signals_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() is None


def test_precedence_differs_from_update_resolver(tmp_path, monkeypatch):
    """Pin that the two resolvers are DELIBERATELY different (spec §5.1/§10):

    data reads prefer the workspace you stand in (cwd before anchor);
    `agnes update` converges the anchor (anchor before cwd). A dedup
    refactor collapsing them would change foreign-repo-safety behavior.
    """
    from cli.commands.update import _resolve_workspace as update_resolve

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    cwd = _make_shaped(tmp_path / "cwd-ws")  # sentinel marker => update sees it too
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    monkeypatch.setattr("cli.commands.update.get_workspace_root", lambda: str(anchor))
    monkeypatch.chdir(cwd)
    assert resolve_data_workspace() == cwd.resolve()  # cwd first
    assert update_resolve() == anchor.resolve()  # anchor first

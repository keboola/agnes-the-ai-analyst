"""Fetching Ossie documents: git clone + glob, upload pass-through, and the
rule that a failed fetch must never look like a source that went empty.

Reuses the `e2e_env` DATA_DIR-isolation fixture under the `system_db` name,
the same adaptation `tests/test_semantic_projection.py` and
`tests/test_semantic_importer.py` made.
"""

import pytest

import src.semantic.transports as transports
from src.semantic.transports import import_source, load_documents


def _doc(model, dataset="orders"):
    return (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        f"  - name: {model}\n"
        "    datasets:\n"
        f"      - name: {dataset}\n"
        f"        source: db.public.{dataset}\n"
    )


DOC = _doc("retail")
DOC_B = _doc("finance", "costs")

GIT_CONFIG = {
    "repo_url": "https://example.com/x.git",
    "ref": "main",
    "glob": "semantic/**/*.yaml",
}
GIT_SOURCE = {"id": "s1", "kind": "git", "adapter": "native", "config": GIT_CONFIG}


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


@pytest.fixture
def clone_dir(tmp_path, monkeypatch):
    """A fake clone: two matching documents, one file the glob must ignore."""
    root = tmp_path / "clone"
    (root / "semantic" / "nested").mkdir(parents=True)
    (root / "semantic" / "a.yaml").write_text(DOC)
    (root / "semantic" / "nested" / "b.yaml").write_text(DOC_B)
    (root / "README.md").write_text("not a model")
    monkeypatch.setattr(transports, "_clone", lambda **kw: root)
    return root


def test_git_transport_globs_matching_files(clone_dir):
    docs = load_documents(GIT_SOURCE)

    assert len(docs) == 2
    assert all("semantic_model" in d for d in docs)


def test_git_transport_rejects_paths_escaping_the_clone(clone_dir):
    """A symlink out of the clone must not be readable through the glob.

    A cloned repository is untrusted input: whoever can push to it chooses
    these filenames.
    """
    (clone_dir / "semantic" / "escape.yaml").symlink_to("/etc/passwd")

    docs = load_documents(GIT_SOURCE)

    assert len(docs) == 2
    assert not any("root:" in d for d in docs)


def test_upload_transport_passes_documents_through():
    docs = load_documents({"id": "s2", "kind": "upload", "adapter": "native", "config": {"documents": [DOC]}})

    assert docs == [DOC], "byte-identical: export hands this text straight back out"


def test_failed_clone_records_the_error_and_imports_nothing(system_db, monkeypatch):
    """An unreachable source must never look like a source that went empty.

    An empty document list means "prune everything", so swallowing a clone
    failure would delete live rows on the next network blip.
    """
    from src.repositories import semantic_model_repo, semantic_source_repo

    semantic_source_repo().create(id="s1", kind="git", name="x", adapter="native", config=GIT_CONFIG)

    def _boom(**kwargs):
        raise RuntimeError("clone failed: host unreachable")

    monkeypatch.setattr(transports, "_clone", _boom)

    with pytest.raises(RuntimeError):
        import_source("s1")

    row = semantic_source_repo().get("s1")
    assert row["last_sync_status"] == "error"
    assert "unreachable" in row["last_sync_error"]
    assert semantic_model_repo().list_all() == []


def test_successful_import_stores_the_model_and_records_ok(system_db, clone_dir):
    from src.repositories import semantic_model_repo, semantic_source_repo

    semantic_source_repo().create(id="s1", kind="git", name="x", adapter="native", config=GIT_CONFIG)

    report = import_source("s1")

    assert report.models_written == 2
    assert {m["slug"] for m in semantic_model_repo().list_all()} == {"retail", "finance"}
    stored = semantic_model_repo().get_by_slug("retail")
    assert stored is not None
    assert stored["source_ref"] == "s1", "prune isolation keys on the registered source"

    row = semantic_source_repo().get("s1")
    assert row["last_sync_status"] == "ok"
    assert row["last_sync_error"] is None


def test_a_later_success_clears_an_earlier_error(system_db, clone_dir):
    """A stale error left behind after a good sync is how a status page lies."""
    from src.repositories import semantic_source_repo

    repo = semantic_source_repo()
    repo.create(id="s1", kind="git", name="x", adapter="native", config=GIT_CONFIG)
    repo.record_sync("s1", status="error", error="boom")

    import_source("s1")

    assert repo.get("s1")["last_sync_error"] is None

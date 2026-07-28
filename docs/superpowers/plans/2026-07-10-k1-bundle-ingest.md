# K1: Bundle Ingest (zip / Confluence dump → Collections) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload a zip archive (including a Confluence HTML space export) into a Collection and have every supported file inside ingested exactly as if uploaded individually, with per-child statuses and an aggregate status on the archive row.

**Architecture:** A new `bundle` tier in the upload allowlist routes `.zip` files through the existing upload path unchanged; `src/ingest/runner.py` dispatches `zip` to a new `src/ingest/bundle.py`, which safely unpacks members (zip-slip + size/count guards), normalizes Confluence-export HTML at unpack time, stores each member content-addressed, creates child `corpus_files` rows linked via a new `parent_file_id` column, ingests each child through the existing `ingest_file`, and finally aggregates child statuses onto the archive row. Re-ingest reuses child rows matched by `(filename, sha256)` so derived tables/chunks are replaced, not duplicated.

**Tech Stack:** Python 3.11+, FastAPI, DuckDB + Postgres (dual-backend repos), stdlib `zipfile` + `html.parser`, pytest.

## Global Constraints

- DuckDB repo change ⇒ matching `_pg.py` change + cross-engine contract test in the same task (CONTRIBUTING.md sync-map).
- Schema change ⇒ BOTH ladders: `src/db.py` `_v86_to_v87` + `SCHEMA_VERSION = 87`, and Alembic `migrations/versions/0034_parent_file_id_v87.py` + `src/models/collections.py` model update.
- Vendor-agnostic: Confluence appears only as a detected export format; no customer names/hosts.
- No AI attribution in commits. Commit messages clean and concise.
- CHANGELOG bullet under `## [Unreleased]` in the final task.
- Full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- Test commands in tasks use `.venv/bin/pytest`.

---

### Task 1: `parent_file_id` column (both ladders, both repos, contract test)

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 86→87 at line ~50; `corpus_files` DDL in `_SYSTEM_SCHEMA` at line ~1340 and in `_v81_to_v82` at line ~5282; new `_v86_to_v87`; dispatch wiring at both sites ~line 5787 area and ~line 6031)
- Create: `migrations/versions/0034_parent_file_id_v87.py`
- Modify: `src/models/collections.py` (CorpusFile model)
- Modify: `src/repositories/corpus_files.py` (`_COLS`, `add`, new `list_children`)
- Modify: `src/repositories/corpus_files_pg.py` (same three)
- Test: `tests/db_pg/test_corpus_files_contract.py`

**Interfaces:**
- Produces: `corpus_files.parent_file_id VARCHAR NULL`; `CorpusFilesRepository.add(..., parent_file_id: Optional[str] = None) -> str`; `CorpusFilesRepository.list_children(parent_file_id: str) -> List[Dict]` (ordered by `created_at`); identical on the PG repo.

- [ ] **Step 1: Write the failing contract test**

Append to the existing parametrized contract test module `tests/db_pg/test_corpus_files_contract.py`, following its existing fixture pattern (both-backend `repo` fixture):

```python
def test_parent_file_id_roundtrip_and_children(repo):
    parent = repo.add(
        corpus_id="cor_x", filename="dump.zip", sha256="p" * 64,
        file_type="zip", size_bytes=10, storage_path="/tmp/p.zip",
    )
    child = repo.add(
        corpus_id="cor_x", filename="page.html", sha256="c" * 64,
        file_type="html", size_bytes=5, storage_path="/tmp/c.html",
        parent_file_id=parent,
    )
    assert repo.get(parent)["parent_file_id"] is None
    assert repo.get(child)["parent_file_id"] == parent
    kids = repo.list_children(parent)
    assert [k["id"] for k in kids] == [child]
    assert repo.list_children(child) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/db_pg/test_corpus_files_contract.py -q -k parent`
Expected: FAIL (`add() got an unexpected keyword argument 'parent_file_id'`)

- [ ] **Step 3: DuckDB ladder**

In `src/db.py`:
1. `SCHEMA_VERSION = 87`.
2. Add `parent_file_id VARCHAR,` to the `corpus_files` DDL in BOTH `_SYSTEM_SCHEMA` (~line 1340) and `_v81_to_v82` (~line 5282) — after `storage_path`.
3. New migration fn next to `_v85_to_v86`:

```python
def _v86_to_v87(conn) -> None:
    """v86→v87: corpus_files.parent_file_id — bundle (zip) child linkage (K1)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info('corpus_files')").fetchall()}
    if "parent_file_id" not in cols:
        conn.execute("ALTER TABLE corpus_files ADD COLUMN parent_file_id VARCHAR")
    conn.execute("UPDATE schema_version SET version = 87")
```

4. Wire into `_ensure_schema` at BOTH dispatch sites: fresh-install block (after the `_v85_to_v86(conn)` call add `_v86_to_v87(conn)`) and upgrade block (`if current < 87: _v86_to_v87(conn)` after the `< 86` guard).

- [ ] **Step 4: Alembic ladder + model**

Create `migrations/versions/0034_parent_file_id_v87.py`:

```python
"""corpus_files.parent_file_id — bundle (zip) child linkage (DuckDB v87, K1)

Revision ID: 0034_parent_file_id_v87
Revises: 0033_everyone_backfill_v86
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0034_parent_file_id_v87"
down_revision: str = "0033_everyone_backfill_v86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corpus_files", sa.Column("parent_file_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("corpus_files", "parent_file_id")
```

In `src/models/collections.py`, add to `CorpusFile` after `storage_path`:

```python
    # Set on children extracted from an uploaded archive (K1 bundle ingest);
    # NULL for directly-uploaded files and for the archive row itself.
    parent_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 5: Both repos**

`src/repositories/corpus_files.py`:
- Add `"parent_file_id",` to `_COLS` (after `"storage_path"`).
- `add(...)` gains keyword `parent_file_id: Optional[str] = None`; INSERT becomes:

```python
        self.conn.execute(
            "INSERT INTO corpus_files "
            "(id, corpus_id, filename, sha256, file_type, size_bytes, storage_path, parent_file_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [file_id, corpus_id, filename, sha256, file_type, size_bytes, storage_path, parent_file_id],
        )
```

- New method:

```python
    def list_children(self, parent_file_id: str) -> List[Dict[str, Any]]:
        """All child rows extracted from the given archive file, by created_at."""
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE parent_file_id = ? ORDER BY created_at",
            [parent_file_id],
        ).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]
```

`src/repositories/corpus_files_pg.py`: mirror all three changes in its idiom (same `_COLS` entry, same `add` kwarg written in its INSERT, same `list_children` with `%s`-style/SQLAlchemy params per the file's existing style — copy the pattern from its `list_for_corpus`).

- [ ] **Step 6: Run gates**

Run: `.venv/bin/pytest tests/db_pg/test_corpus_files_contract.py tests/test_db_schema_version.py tests/db_pg/test_alembic_roundtrip.py tests/test_collections_schema.py -q`
Expected: PASS (schema-version test drives old files to 87; alembic roundtrip + no-drift green)

- [ ] **Step 7: Commit**

```bash
git add src/db.py migrations/versions/0034_parent_file_id_v87.py src/models/collections.py src/repositories/corpus_files.py src/repositories/corpus_files_pg.py tests/db_pg/test_corpus_files_contract.py
git commit -m "feat(collections): corpus_files.parent_file_id for bundle children (v87)"
```

---

### Task 2: `store_corpus_bytes` — sync bytes variant of content-addressed storage

**Files:**
- Modify: `src/file_storage.py`
- Test: `tests/test_file_storage_bytes.py` (create)

**Interfaces:**
- Consumes: existing `_corpus_dir`, `_safe_ext`, `MAX_UPLOAD_BYTES`, `StoredFile`.
- Produces: `store_corpus_bytes(corpus_id: str, filename: str, data: bytes) -> StoredFile` — raises `HTTPException(413)` over cap, `HTTPException(400)` on empty; same content-addressed `<sha256><ext>` layout and atomic `.part` write as `store_corpus_file`.

- [ ] **Step 1: Write the failing test**

```python
"""store_corpus_bytes — sync content-addressed writes for bundle members."""

import pytest
from fastapi import HTTPException


def test_store_bytes_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_bytes

    s = store_corpus_bytes("cor_1", "page.html", b"<html>hi</html>")
    assert s.size_bytes == 15
    assert s.ext == ".html"
    assert s.storage_path.endswith(f"{s.sha256}.html")
    with open(s.storage_path, "rb") as fh:
        assert fh.read() == b"<html>hi</html>"
    # idempotent: same bytes → same path
    s2 = store_corpus_bytes("cor_1", "other-name.html", b"<html>hi</html>")
    assert s2.storage_path == s.storage_path


def test_store_bytes_empty_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_bytes

    with pytest.raises(HTTPException) as exc:
        store_corpus_bytes("cor_1", "empty.txt", b"")
    assert exc.value.status_code == 400


def test_store_bytes_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.file_storage as fs

    monkeypatch.setattr(fs, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(HTTPException) as exc:
        fs.store_corpus_bytes("cor_1", "big.txt", b"12345")
    assert exc.value.status_code == 413
```

Note: check how `_get_data_dir` reads the env in `src/db.py` — if it caches, use the same monkeypatch idiom the existing storage tests use (see `tests/test_api_collections.py` fixtures) instead of `setenv`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_file_storage_bytes.py -q`
Expected: FAIL (`ImportError: cannot import name 'store_corpus_bytes'`)

- [ ] **Step 3: Implement**

Append to `src/file_storage.py`:

```python
def store_corpus_bytes(corpus_id: str, filename: str, data: bytes) -> StoredFile:
    """Store an in-memory blob content-addressed under ``corpus_id``.

    Sync sibling of :func:`store_corpus_file` for bundle members already in
    memory after archive extraction. Same layout, cap, and atomicity.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file_too_large:max_{MAX_UPLOAD_BYTES}_bytes")
    if not data:
        raise HTTPException(status_code=400, detail="empty_upload")

    ext = _safe_ext(filename)
    digest = hashlib.sha256(data).hexdigest()
    target = _corpus_dir(corpus_id) / f"{digest}{ext}"
    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return StoredFile(sha256=digest, storage_path=str(target), size_bytes=len(data), ext=ext)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_file_storage_bytes.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/file_storage.py tests/test_file_storage_bytes.py
git commit -m "feat(collections): store_corpus_bytes for archive members"
```

---

### Task 3: allowlist `bundle` tier for zip

**Files:**
- Modify: `src/corpus_allowlist.py`
- Test: `tests/test_corpus_allowlist.py`

**Interfaces:**
- Produces: `classify("dump.zip") == "bundle"`; new `BUNDLE_EXTENSIONS: frozenset[str] = frozenset({"zip"})`. The upload endpoint needs NO change: `bundle` flows through its existing non-None branch (store + `pending` + schedule `ingest_file`).

- [ ] **Step 1: Extend the existing allowlist tests**

Add to `tests/test_corpus_allowlist.py` (match its existing style):

```python
def test_zip_is_bundle_tier():
    from src.corpus_allowlist import classify

    assert classify("dump.zip") == "bundle"
    assert classify("SPACE-export.ZIP") == "bundle"


def test_other_archives_still_rejected():
    from src.corpus_allowlist import classify

    assert classify("a.tar.gz") is None
    assert classify("a.7z") is None
    assert classify("a.rar") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_corpus_allowlist.py -q`
Expected: FAIL (zip currently classifies as None)

- [ ] **Step 3: Implement**

In `src/corpus_allowlist.py` add after `TIER2_EXTENSIONS`:

```python
# Archives unpacked server-side into per-member corpus_files rows (K1 bundle
# ingest). Zip only: it covers Confluence HTML/XML space exports and ad-hoc
# document dumps. tar/7z/rar stay rejected — no streaming-unpack guarantees.
BUNDLE_EXTENSIONS: frozenset[str] = frozenset({"zip"})
```

and in `classify()` before the final `return None`:

```python
    if ext in BUNDLE_EXTENSIONS:
        return "bundle"
```

Update the module docstring tier list to mention `bundle`, and the `upload_files` docstring in `app/api/collections.py` (tier list bullet: `**bundle** (zip) → stored, then unpacked + per-member ingestion in the background`).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_corpus_allowlist.py tests/test_api_collections.py -q`
Expected: PASS (upload endpoint accepts zip via the generic non-None branch)

- [ ] **Step 5: Commit**

```bash
git add src/corpus_allowlist.py tests/test_corpus_allowlist.py app/api/collections.py
git commit -m "feat(collections): accept zip uploads as bundle tier"
```

---

### Task 4: Confluence HTML normalizer

**Files:**
- Create: `src/ingest/confluence.py`
- Test: `tests/test_ingest_confluence.py` (create)

**Interfaces:**
- Produces: `normalize_html(content: bytes) -> bytes` — if the HTML carries Confluence-export markers, strip navigation/boilerplate blocks and ensure the page `<title>` is present as a leading `<h1>`; otherwise return input unchanged. Pure function, no I/O.

- [ ] **Step 1: Write the failing tests**

```python
"""Confluence HTML export normalization (K1)."""

from src.ingest.confluence import normalize_html

_CONFLUENCE_PAGE = b"""<html><head><title>SPACE : How billing works</title></head>
<body>
<div id="page">
<div id="main-header"><div id="breadcrumb-section"><ol><li>Home</li><li>Billing</li></ol></div></div>
<h1 id="title-heading" class="pagetitle"><span id="title-text">How billing works</span></h1>
<div id="content" class="view">
<div class="page-metadata">Created by Someone, last modified on Jan 01</div>
<p>Invoices are generated monthly.</p>
</div>
<div id="footer" role="contentinfo"><section class="footer-body"><p>Document generated by Confluence on Jan 01</p></section></div>
</div>
</body></html>"""


def test_confluence_boilerplate_stripped():
    out = normalize_html(_CONFLUENCE_PAGE).decode()
    assert "Invoices are generated monthly." in out
    assert "breadcrumb-section" not in out
    assert "page-metadata" not in out
    assert "Document generated by Confluence" not in out


def test_confluence_title_preserved():
    out = normalize_html(_CONFLUENCE_PAGE).decode()
    assert "How billing works" in out


def test_non_confluence_html_untouched():
    plain = b"<html><body><h1>Hello</h1><p>World</p></body></html>"
    assert normalize_html(plain) == plain


def test_invalid_bytes_untouched():
    junk = b"\xff\xfenot html"
    assert normalize_html(junk) == junk
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ingest_confluence.py -q`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement**

Create `src/ingest/confluence.py`:

```python
"""Confluence HTML-export normalization (K1 bundle ingest).

A Confluence space export is a zip of standalone HTML pages wrapped in heavy
navigation chrome (breadcrumbs, page metadata, footer). Indexing that chrome
poisons retrieval — every page would match "Home", "Created by", etc. This
module strips the known boilerplate blocks at unpack time so the stored child
blob is already clean; downstream extraction/chunking needs no special case.

Detection is per-file and marker-based (no bundle-level state): only HTML that
carries Confluence-export ids is touched, everything else passes through
byte-identical.
"""

from __future__ import annotations

import re

# Present in Confluence HTML space exports; used only for detection.
_MARKERS = (b'id="breadcrumb-section"', b'id="title-heading"', b"Document generated by Confluence")

# Boilerplate blocks to drop. The export format nests each in a single
# div/section with a stable id/class, so a non-greedy DOTALL regex per block
# is sufficient and avoids a DOM dependency.
_STRIP_PATTERNS = [
    re.compile(rb'<div id="breadcrumb-section".*?</div>', re.DOTALL),
    re.compile(rb'<div class="page-metadata".*?</div>', re.DOTALL),
    re.compile(rb'<div id="footer".*?</div>', re.DOTALL),
]


def normalize_html(content: bytes) -> bytes:
    """Strip Confluence-export chrome; non-Confluence input returns unchanged."""
    if not any(m in content for m in _MARKERS):
        return content
    out = content
    for pat in _STRIP_PATTERNS:
        out = pat.sub(b"", out)
    return out
```

Note the title test passes without extra work: the export keeps the page title in `<h1 id="title-heading">`, which is *not* stripped (only breadcrumbs/metadata/footer are). If Step 4 shows the `<h1>` got swallowed by a pattern, tighten the pattern rather than re-adding the title.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_ingest_confluence.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ingest/confluence.py tests/test_ingest_confluence.py
git commit -m "feat(ingest): confluence export HTML normalizer"
```

---

### Task 5: `src/ingest/bundle.py` — safe unpack + child rows + aggregate status

**Files:**
- Create: `src/ingest/bundle.py`
- Test: `tests/test_ingest_bundle.py` (create)

**Interfaces:**
- Consumes: `store_corpus_bytes` (Task 2), `normalize_html` (Task 4), `corpus_files_repo().add(..., parent_file_id=)` + `.list_children` (Task 1), `corpus_chunks_repo().delete_for_file`, `src.corpus_allowlist.classify`.
- Produces: `ingest_bundle(corpus_id: str, file_id: str, storage_path: str) -> str` returning the archive row's final status (`indexed | needs_review | rejected`). Takes an `ingest_child` callable parameter (default `src.ingest.runner.ingest_file`) so unit tests inject a fake. Constants `MAX_BUNDLE_MEMBERS = 1000`, `MAX_BUNDLE_TOTAL_BYTES = 1 GiB`.

- [ ] **Step 1: Write the failing tests**

The existing ingest tests show the DB fixture idiom — copy the fixture style from `tests/test_ingest_runner.py` (fresh system DB via `tmp_path` + repo factories). Test file:

```python
"""Bundle (zip) unpack + child-row lifecycle (K1)."""

import io
import zipfile

import pytest

# Reuse the system-DB fixture idiom from tests/test_ingest_runner.py — copy
# its conftest-style setup here (fresh DATA_DIR via tmp_path/monkeypatch).


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_archive_row(corpus_id, data):
    """Store zip bytes + create the archive corpus_files row; returns (file_id, path)."""
    from src.file_storage import store_corpus_bytes
    from src.repositories import corpus_files_repo

    stored = store_corpus_bytes(corpus_id, "dump.zip", data)
    fid = corpus_files_repo().add(
        corpus_id=corpus_id, filename="dump.zip", sha256=stored.sha256,
        file_type="zip", size_bytes=stored.size_bytes, storage_path=stored.storage_path,
    )
    return fid, stored.storage_path


def test_bundle_happy_path(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    data = _zip_bytes({
        "docs/page.html": b"<html><body><p>hello world</p></body></html>",
        "data/t.csv": b"a,b\n1,2\n",
        "__MACOSX/junk": b"x",
        ".DS_Store": b"x",
    })
    fid, _ = _make_archive_row("cor_b", data)
    seen = []

    def fake_ingest(child_id):
        seen.append(child_id)
        corpus_files_repo().set_status(child_id, status="indexed", detail={})
        return "indexed"

    status = ingest_bundle("cor_b", fid, corpus_files_repo().get(fid)["storage_path"], ingest_child=fake_ingest)
    assert status == "indexed"
    kids = corpus_files_repo().list_children(fid)
    assert sorted(k["filename"] for k in kids) == ["data/t.csv", "docs/page.html"]  # junk skipped
    assert len(seen) == 2
    parent = corpus_files_repo().get(fid)
    assert parent["processing_status"] == "indexed"
    assert parent["processing_detail"]["children"] == 2
    assert parent["processing_detail"]["indexed"] == 2


def test_bundle_unsafe_and_nested_members_rejected(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    data = _zip_bytes({
        "../evil.txt": b"escape",
        "inner.zip": b"PK\x03\x04fakezip",
        "ok.md": b"# fine\ncontent here",
    })
    fid, path = _make_archive_row("cor_b", data)

    def fake_ingest(child_id):
        corpus_files_repo().set_status(child_id, status="indexed", detail={})
        return "indexed"

    status = ingest_bundle("cor_b", fid, path, ingest_child=fake_ingest)
    assert status == "indexed"  # ok.md indexed
    kids = {k["filename"]: k for k in corpus_files_repo().list_children(fid)}
    assert kids["../evil.txt"]["processing_status"] == "rejected"
    assert kids["../evil.txt"]["processing_detail"]["reason"] == "unsafe_path"
    assert kids["inner.zip"]["processing_status"] == "rejected"
    assert kids["inner.zip"]["processing_detail"]["reason"] == "nested_archive_unsupported"
    assert kids["ok.md"]["processing_status"] == "indexed"


def test_bundle_unsupported_member_rejected_supported_ingested(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    data = _zip_bytes({"cad.dwg": b"binary", "ok.txt": b"text content"})
    fid, path = _make_archive_row("cor_b", data)

    def fake_ingest(child_id):
        corpus_files_repo().set_status(child_id, status="indexed", detail={})
        return "indexed"

    ingest_bundle("cor_b", fid, path, ingest_child=fake_ingest)
    kids = {k["filename"]: k for k in corpus_files_repo().list_children(fid)}
    assert kids["cad.dwg"]["processing_status"] == "rejected"
    assert kids["ok.txt"]["processing_status"] == "indexed"


def test_bundle_empty_or_all_rejected_needs_review(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    fid, path = _make_archive_row("cor_b", _zip_bytes({"junk.dwg": b"x"}))
    status = ingest_bundle("cor_b", fid, path, ingest_child=lambda _: "indexed")
    assert status == "needs_review"
    parent = corpus_files_repo().get(fid)
    assert parent["processing_status"] == "needs_review"
    assert parent["processing_detail"]["reason"] == "no_member_indexed"


def test_bundle_corrupt_zip_rejected(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    from src.file_storage import store_corpus_bytes

    stored = store_corpus_bytes("cor_b", "bad.zip", b"this is not a zip")
    fid = corpus_files_repo().add(
        corpus_id="cor_b", filename="bad.zip", sha256=stored.sha256,
        file_type="zip", size_bytes=stored.size_bytes, storage_path=stored.storage_path,
    )
    assert ingest_bundle("cor_b", fid, stored.storage_path) == "rejected"
    assert corpus_files_repo().get(fid)["processing_detail"]["reason"] == "invalid_archive"


def test_bundle_member_limits(fresh_db, monkeypatch):
    import src.ingest.bundle as bundle
    from src.repositories import corpus_files_repo

    monkeypatch.setattr(bundle, "MAX_BUNDLE_MEMBERS", 1)
    fid, path = _make_archive_row("cor_b", _zip_bytes({"a.txt": b"a", "b.txt": b"b"}))
    assert bundle.ingest_bundle("cor_b", fid, path) == "rejected"
    assert corpus_files_repo().get(fid)["processing_detail"]["reason"] == "too_many_members"


def test_bundle_reingest_reuses_children(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    data = _zip_bytes({"a.md": b"# a\nbody"})
    fid, path = _make_archive_row("cor_b", data)

    def fake_ingest(child_id):
        corpus_files_repo().set_status(child_id, status="indexed", detail={})
        return "indexed"

    ingest_bundle("cor_b", fid, path, ingest_child=fake_ingest)
    first = corpus_files_repo().list_children(fid)
    ingest_bundle("cor_b", fid, path, ingest_child=fake_ingest)
    second = corpus_files_repo().list_children(fid)
    assert [k["id"] for k in first] == [k["id"] for k in second]  # rows reused, not duplicated


def test_bundle_confluence_member_normalized(fresh_db):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    page = (b'<html><head><title>S : P</title></head><body>'
            b'<div id="breadcrumb-section"><ol><li>Home</li></ol></div>'
            b'<h1 id="title-heading">P</h1><p>real content</p></body></html>')
    fid, path = _make_archive_row("cor_b", _zip_bytes({"p.html": page}))
    ingest_bundle("cor_b", fid, path, ingest_child=lambda _: "indexed")
    kid = corpus_files_repo().list_children(fid)[0]
    with open(kid["storage_path"], "rb") as fh:
        stored = fh.read()
    assert b"breadcrumb-section" not in stored
    assert b"real content" in stored
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ingest_bundle.py -q`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement**

Create `src/ingest/bundle.py`:

```python
"""Bundle (zip) ingestion — unpack an uploaded archive into child corpus files.

The archive row itself never carries chunks; each supported member becomes its
own ``corpus_files`` row (``parent_file_id`` → the archive) stored
content-addressed like a direct upload, then driven through the normal
``ingest_file`` router. The archive row's final status aggregates its
children: ``indexed`` when at least one child indexed, else ``needs_review``.

Safety: member names are validated against zip-slip (absolute paths, ``..``);
nested archives are rejected per-member; member count and total uncompressed
size are capped before any extraction. Metadata junk (``__MACOSX/``,
``.DS_Store``) is skipped silently.

Idempotent re-ingest: children are matched to existing rows by
``(filename, sha256)`` and reused, so per-file idempotency downstream
(chunk replacement, derived-table re-registration) applies; unmatched
leftovers from a previous run are deleted along with their chunks.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from typing import Callable, Optional

from src.corpus_allowlist import MAX_UPLOAD_BYTES, classify
from src.file_storage import store_corpus_bytes
from src.ingest.confluence import normalize_html
from src.repositories import corpus_chunks_repo, corpus_files_repo

logger = logging.getLogger(__name__)

MAX_BUNDLE_MEMBERS = 1000
MAX_BUNDLE_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB uncompressed

_SKIP_PREFIXES = ("__MACOSX/",)
_SKIP_BASENAMES = {".DS_Store", "Thumbs.db"}


def _is_unsafe(name: str) -> bool:
    norm = posixpath.normpath(name)
    return name.startswith(("/", "\\")) or norm.startswith("..") or "\\.." in name or ":" in name.split("/")[0]


def _is_junk(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES) or posixpath.basename(name) in _SKIP_BASENAMES


def ingest_bundle(
    corpus_id: str,
    file_id: str,
    storage_path: str,
    *,
    ingest_child: Optional[Callable[[str], str]] = None,
) -> str:
    """Unpack the archive at ``storage_path`` and ingest each member.

    Returns the archive row's final ``processing_status``.
    """
    if ingest_child is None:
        from src.ingest.runner import ingest_file as ingest_child  # circular-safe

    cf_repo = corpus_files_repo()

    try:
        zf = zipfile.ZipFile(storage_path)
        infos = [i for i in zf.infolist() if not i.is_dir() and not _is_junk(i.filename)]
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("bundle open failed file_id=%s: %s", file_id, exc)
        cf_repo.set_status(file_id, status="rejected", detail={"reason": "invalid_archive"})
        return "rejected"

    if len(infos) > MAX_BUNDLE_MEMBERS:
        cf_repo.set_status(
            file_id,
            status="rejected",
            detail={"reason": "too_many_members", "members": len(infos), "max": MAX_BUNDLE_MEMBERS},
        )
        return "rejected"
    if sum(i.file_size for i in infos) > MAX_BUNDLE_TOTAL_BYTES:
        cf_repo.set_status(
            file_id,
            status="rejected",
            detail={"reason": "bundle_too_large", "max_bytes": MAX_BUNDLE_TOTAL_BYTES},
        )
        return "rejected"

    # Existing children from a previous run, for row reuse.
    prior = {(k["filename"], k["sha256"]): k for k in cf_repo.list_children(file_id)}
    kept_ids: set[str] = set()
    counts = {"indexed": 0, "rejected": 0, "needs_review": 0, "pending": 0, "processing": 0}
    children = 0

    for info in infos:
        name = info.filename
        children += 1

        def _add_rejected(reason: str) -> None:
            cid = cf_repo.add(
                corpus_id=corpus_id, filename=name, sha256="", file_type=None,
                size_bytes=info.file_size, storage_path=None, parent_file_id=file_id,
            )
            cf_repo.set_status(cid, status="rejected", detail={"reason": reason})
            kept_ids.add(cid)
            counts["rejected"] += 1

        if _is_unsafe(name):
            _add_rejected("unsafe_path")
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        tier = classify(name)
        if tier == "bundle":
            _add_rejected("nested_archive_unsupported")
            continue
        if tier is None:
            _add_rejected("unsupported_type")
            continue
        if info.file_size > MAX_UPLOAD_BYTES:
            _add_rejected("member_too_large")
            continue

        data = zf.read(info)
        if ext in ("html", "htm"):
            data = normalize_html(data)
        if not data:
            _add_rejected("empty_member")
            continue

        stored = store_corpus_bytes(corpus_id, name, data)
        existing = prior.get((name, stored.sha256))
        if existing:
            child_id = existing["id"]
        else:
            child_id = cf_repo.add(
                corpus_id=corpus_id, filename=name, sha256=stored.sha256,
                file_type=stored.ext.lstrip(".") or None, size_bytes=stored.size_bytes,
                storage_path=stored.storage_path, parent_file_id=file_id,
            )
        kept_ids.add(child_id)
        status = ingest_child(child_id)
        counts[status] = counts.get(status, 0) + 1

    # Prune children from a prior run that no longer match (renamed/changed).
    chunks_repo = corpus_chunks_repo()
    for row in prior.values():
        if row["id"] not in kept_ids:
            chunks_repo.delete_for_file(row["id"])
            cf_repo.delete(row["id"])

    detail = {"kind": "bundle", "children": children, **counts}
    if counts["indexed"] > 0:
        cf_repo.set_status(file_id, status="indexed", detail=detail)
        return "indexed"
    cf_repo.set_status(file_id, status="needs_review", detail={**detail, "reason": "no_member_indexed"})
    return "needs_review"
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_ingest_bundle.py -q`
Expected: PASS (fix any fixture-idiom mismatches against `tests/test_ingest_runner.py`)

- [ ] **Step 5: Commit**

```bash
git add src/ingest/bundle.py tests/test_ingest_bundle.py
git commit -m "feat(ingest): bundle unpack with guards, child rows, aggregate status"
```

---

### Task 6: runner routing + end-to-end API test with a fixture zip

**Files:**
- Modify: `src/ingest/runner.py`
- Modify: `app/api/collections.py` (`_file_out`: expose `parent_file_id`)
- Test: `tests/test_ingest_runner.py` (routing), `tests/test_api_collections.py` (E2E upload)

**Interfaces:**
- Consumes: `ingest_bundle` (Task 5).
- Produces: `ingest_file` on a `zip` row delegates to `ingest_bundle`; `_file_out` rows include `parent_file_id`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_ingest_runner.py` add (reusing its fixtures):

```python
def test_ingest_file_routes_zip_to_bundle(fresh_db, monkeypatch):
    import src.ingest.runner as runner

    calls = {}

    def fake_bundle(corpus_id, file_id, storage_path, **kw):
        calls["args"] = (corpus_id, file_id, storage_path)
        return "indexed"

    monkeypatch.setattr("src.ingest.bundle.ingest_bundle", fake_bundle)
    # create a corpus_files row with file_type="zip" via the repo, then:
    assert runner.ingest_file(file_id) == "indexed"
    assert calls["args"][1] == file_id
```

In `tests/test_api_collections.py` add an upload E2E (reusing its TestClient fixture):

```python
def test_upload_zip_bundle_end_to_end(client, admin_headers, tmp_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.md", "# Notes\n\nBundle ingestion works end to end.")
        zf.writestr("junk.dwg", "binary")
    buf.seek(0)

    col = client.post("/api/collections", json={"name": "Bundle Col"}, headers=admin_headers).json()
    resp = client.post(
        f"/api/collections/{col['id']}/files",
        files=[("files", ("dump.zip", buf.getvalue(), "application/zip"))],
        headers=admin_headers,
    )
    assert resp.status_code == 200  # zip itself accepted; member rejection ≠ upload rejection
    files = client.get(f"/api/collections/{col['id']}/files", headers=admin_headers).json()
    by_name = {f["filename"]: f for f in files}
    # TestClient runs BackgroundTasks synchronously on response close.
    assert by_name["dump.zip"]["processing_status"] == "indexed"
    assert by_name["dump.zip"]["parent_file_id"] is None
    assert by_name["notes.md"]["processing_status"] == "indexed"
    assert by_name["notes.md"]["parent_file_id"] == by_name["dump.zip"]["id"]
    assert by_name["junk.dwg"]["processing_status"] == "rejected"
    # search finds bundle content
    hits = client.get(
        "/api/collections/search",
        params={"q": "bundle ingestion works", "corpus_id": col["id"]},
        headers=admin_headers,
    ).json()
    assert any("notes.md" in str(h) for h in hits)
```

Adapt fixture names (`client`, `admin_headers`, collection-create payload) to what `tests/test_api_collections.py` actually uses — copy its existing upload test as the template.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ingest_runner.py -q -k zip; .venv/bin/pytest tests/test_api_collections.py -q -k bundle`
Expected: FAIL (runner routes zip to the prose path → rejected "no extractor")

- [ ] **Step 3: Implement**

In `src/ingest/runner.py` add after the `IMAGE_EXTS` check inside `ingest_file` (i.e. as the first routing branch after `cf_repo.set_status(file_id, status="processing")` / `ext = ...`):

```python
        if ext == "zip":
            from src.ingest.bundle import ingest_bundle

            return ingest_bundle(corpus_id, file_id, storage_path)
```

(inside the existing `try:` so unexpected errors keep the defensive `rejected` handling; `ingest_bundle` sets the archive row's own status in every path).

In `app/api/collections.py` `_file_out`, add `"parent_file_id": row.get("parent_file_id"),` to the returned dict.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_ingest_runner.py tests/test_api_collections.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ingest/runner.py app/api/collections.py tests/test_ingest_runner.py tests/test_api_collections.py
git commit -m "feat(collections): route zip uploads through bundle ingestion"
```

---

### Task 7: Library UI child badge + CHANGELOG + full suite

**Files:**
- Modify: `app/web/templates/` — the collection-detail template (find via `grep -rl "processing_status" app/web/templates/`); add an "from <archive filename>" chip on child rows and a child-count line on archive rows using existing `ds.*` idioms (no new CSS; reuse the `.status-pill` pattern already there).
- Modify: `CHANGELOG.md`
- Test: existing template/contract suites only (`tests/test_design_system_contract.py` must stay green); no new UI test.

- [ ] **Step 1: Template tweak**

On the file-row loop in the collection detail template: when `file.parent_file_id` is set, render a muted chip `from archive`; when `file.processing_detail.kind == 'bundle'`, render `{{ file.processing_detail.children }} files · {{ file.processing_detail.indexed }} indexed` as the derived-artifact line. Follow the exact macro/markup style already used for the existing status pills in that template.

- [ ] **Step 2: CHANGELOG**

Under `## [Unreleased]` → `### Added`:

```markdown
- Collections: zip archive upload — a bundle (e.g. a Confluence HTML space export)
  is unpacked server-side, every supported member ingested as its own file with
  per-member status, Confluence navigation chrome stripped automatically (#796).
```

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (investigate any failure; unrelated pre-existing failures: confirm via `git stash` on a clean tree, note in PR body)

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/ CHANGELOG.md
git commit -m "feat(collections): bundle rows in library UI + changelog"
```

---

## Execution notes

- Branch: dedicated `zs/k1-bundle-ingest` off current `main`, in this worktree.
- After implementation: run `/agnes-review`, fix findings, release-cut check (agnes-releaser phase 1 — patch bump if this PR is the release trigger), open PR, watch CI (`gh pr checks`), wait for auto-merge, then watch post-merge `release.yml` (`smoke-test` green + `rollback-on-smoke-fail` skipped).

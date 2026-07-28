# Ingestion Status Honesty (Part A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make collection ingestion honest — empty extractions become `needs_review` (never `indexed`), PDFs work out of the box via core `pypdf`, the Library UI shows failure reasons, and admins can re-ingest a file after a fix.

**Architecture:** Part A of `docs/superpowers/specs/2026-07-08-llm-first-ingestion-design.md`. All changes ride existing plumbing: the `src/ingest/runner.py` status machine gains one state (`needs_review`), `src/ingest/tabular.py` refuses to register empty tables, a new REST endpoint `POST /api/collections/{cid}/files/{fid}/reingest` reuses the existing purge helper + background ingest, with CLI + MCP siblings per the triple-surface ratchet.

**Tech Stack:** Python 3.11, FastAPI, DuckDB, pytest (`e2e_env` fixture), Typer CLI, FastMCP.

**Worktree note:** execute in this worktree (branch `ZS/wizardly-heyrovsky-f6bfa8`). Run tests with `.venv/bin/pytest` (symlinked venv).

---

### Task 1: pypdf as a core dependency

`src/ingest/text_extract.py:96-129` already prefers docling → pypdf → reject. Only the dependency is missing, so default installs reject every PDF.

**Files:**
- Modify: `pyproject.toml` (core `dependencies` list)
- Test: `tests/test_ingest_text_extract.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest_text_extract.py`:

```python
def test_pypdf_is_a_core_dependency(tmp_path):
    """Default installs must extract PDF text without the docling extra.

    Guards the dependency, not pypdf itself: a minimal one-page PDF with a
    text content stream must round-trip through extract_text.
    """
    import pypdf  # noqa: F401  — core dep, not an extra

    # Minimal valid PDF: one page, Helvetica, "Hello Agnes" via a Tj operator.
    content = b"BT /F1 24 Tf 72 720 Td (Hello Agnes) Tj ET"
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"5 0 obj<</Length " + str(len(content)).encode() + b">>stream\n"
        + content
        + b"\nendstream endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    )
    path = tmp_path / "hello.pdf"
    path.write_bytes(pdf)

    result = extract_text(str(path), "pdf")
    assert "Hello Agnes" in result
```

(`extract_text` is already imported at the top of this test module.)

- [ ] **Step 2: Run it — expect FAIL**

```bash
.venv/bin/pytest tests/test_ingest_text_extract.py::test_pypdf_is_a_core_dependency -q
```

Expected: `ModuleNotFoundError: No module named 'pypdf'` (or `UnsupportedDocument` if docling is also absent). If it PASSES, pypdf leaked into the venv from an extra — verify `grep pypdf pyproject.toml` shows it only under the extra, then continue (the dep move is still required).

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, append to the core `dependencies = [...]` list (keep alphabetical ordering if present):

```toml
    "pypdf>=5.0",
```

If `pypdf` is currently listed inside the `docling`/optional extra, leave the extra untouched (harmless duplicate) — the point is the core list.

- [ ] **Step 4: Install + re-run — expect PASS**

```bash
uv pip install -e ".[dev]" && .venv/bin/pytest tests/test_ingest_text_extract.py -q
```

Expected: all tests in the module PASS (the existing `test_pdf_without_reader_raises_unsupported` still passes — it monkeypatches both readers away).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_ingest_text_extract.py
git commit -m "fix(ingest): make pypdf a core dependency so default installs read PDFs"
```

---

### Task 2: `needs_review` status — empty extractions stop lying

Status set becomes `pending → processing → indexed | needs_review | rejected`. Two triggers: a tabular ingest producing 0 rows, and a document/image ingest producing 0 chunks.

**Files:**
- Modify: `src/ingest/tabular.py` (new `EmptyExtraction`, raise on 0 rows before `_meta`/registry writes)
- Modify: `src/ingest/runner.py` (catch it; 0-chunk branches; module docstring line 6)
- Modify: `src/repositories/corpus_files.py:4` (docstring status set)
- Test: `tests/test_ingest_runner.py`, `tests/db_pg/test_corpus_files_contract.py`

- [ ] **Step 1: Write the failing runner tests**

Append to `tests/test_ingest_runner.py`:

```python
def test_empty_tabular_is_needs_review_not_indexed(e2e_env, tmp_path):
    """Header-only CSV → 0 rows: must NOT register a table nor claim indexed."""
    from src.ingest.runner import ingest_file
    from src.repositories import corpus_files_repo, table_registry_repo

    corpus_id = _new_corpus("ing-empty-csv")
    csv = tmp_path / "empty.csv"
    csv.write_text("a,b\n", encoding="utf-8")
    file_id = _add_file(corpus_id, "empty.csv", "csv", str(csv))

    assert ingest_file(file_id) == "needs_review"
    row = corpus_files_repo().get(file_id)
    assert row["processing_status"] == "needs_review"
    assert "empty" in row["processing_detail"]["reason"]
    # No derived table may leak into the registry.
    fid_suffix = file_id.replace("cf_", "")[:8]
    leaked = [
        r for r in table_registry_repo().list_by_source("collection")
        if r.get("id", "").endswith(fid_suffix)
    ]
    assert leaked == []


def test_zero_chunk_document_is_needs_review(e2e_env, tmp_path, monkeypatch):
    """Extractor succeeds but yields no text → needs_review, not indexed."""
    import src.ingest.runner as runner_mod
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("ing-zero-chunks")
    doc = tmp_path / "blank.txt"
    doc.write_text("", encoding="utf-8")
    file_id = _add_file(corpus_id, "blank.txt", "txt", str(doc))

    monkeypatch.setattr(runner_mod, "extract_text", lambda p, t: "")
    assert runner_mod.ingest_file(file_id) == "needs_review"
    row = corpus_files_repo().get(file_id)
    assert row["processing_status"] == "needs_review"
    assert row["processing_detail"]["reason"] == "extraction produced no text chunks"
```

- [ ] **Step 2: Run them — expect FAIL**

```bash
.venv/bin/pytest tests/test_ingest_runner.py -q -k "needs_review"
```

Expected: both FAIL with `assert 'indexed' == 'needs_review'`.

- [ ] **Step 3: Implement in tabular.py**

In `src/ingest/tabular.py`, add next to the existing exception(s) near the top:

```python
class EmptyExtraction(Exception):
    """Parsing succeeded but produced an empty table (0 rows).

    Distinct from UnsupportedTabular (→ rejected): the format was readable,
    the content just didn't survive — the file needs a human or a smarter
    extractor (→ needs_review), not a permanent reject.
    """
```

In `ingest_tabular`, immediately after the row count at `src/ingest/tabular.py:111` (`rows = con.execute(...)`) and **before** the `_meta` insert and registry write:

```python
    if rows == 0:
        pq_path.unlink(missing_ok=True)  # don't leave an orphan parquet
        raise EmptyExtraction(
            f"extraction produced empty table (0 rows) from {filename!r}"
        )
```

(Use the actual local variable holding the parquet path in that function — it appears in the `read_parquet('{safe_pq}')` call; unlink the `Path` it was built from.)

- [ ] **Step 4: Implement in runner.py**

In `src/ingest/runner.py`:

1. Module docstring line 6: `indexed | rejected` → `indexed | needs_review | rejected`.
2. Import: `from src.ingest.tabular import EmptyExtraction, ingest_tabular` (extend the existing import).
3. Document branch (`runner.py:126-133`) — replace the unconditional `indexed` write:

```python
        result = extract_text(storage_path, file_type)
        n, embedded = _chunk_embed_store(corpus_id, file_id, result)
        if n == 0:
            cf_repo.set_status(
                file_id,
                status="needs_review",
                detail={"tier": 1, "kind": "document",
                        "reason": "extraction produced no text chunks"},
            )
            return "needs_review"
        cf_repo.set_status(
            file_id,
            status="indexed",
            detail={"tier": 1, "kind": "document", "chunk_count": n, "embedded": embedded},
        )
        return "indexed"
```

4. Image branch — same guard after its `_chunk_embed_store` call (`runner.py:117`), with `"kind": "image"` and the same reason string.
5. New except clause **before** the generic `except Exception` (order matters — `EmptyExtraction` must not fall into the rejected bucket):

```python
    except EmptyExtraction as exc:
        cf_repo.set_status(file_id, status="needs_review", detail={"reason": str(exc)})
        return "needs_review"
```

6. Update `src/repositories/corpus_files.py:4` docstring: `pending → processing → indexed | needs_review | rejected`.

- [ ] **Step 5: Run — expect PASS**

```bash
.venv/bin/pytest tests/test_ingest_runner.py -q
```

Expected: whole module PASS (existing tests unaffected — non-empty fixtures).

- [ ] **Step 6: Contract test — status round-trips on both backends**

Append to `tests/db_pg/test_corpus_files_contract.py` (same fixtures/parametrization as `test_add_default_status_is_pending` at line 117 — copy its `repo` fixture usage exactly):

```python
def test_set_status_needs_review_roundtrip(repo):
    """`needs_review` (status-honesty, spec 2026-07-08) persists with its reason."""
    fid = repo.add(
        corpus_id="col_x", filename="empty.xlsx", sha256="s1",
        file_type="xlsx", size_bytes=1, storage_path="/tmp/empty.xlsx",
    )
    repo.set_status(fid, status="needs_review",
                    detail={"reason": "extraction produced empty table"})
    row = repo.get(fid)
    assert row["processing_status"] == "needs_review"
    assert row["processing_detail"]["reason"] == "extraction produced empty table"
```

(Adjust the `repo.add(...)` kwargs to match the module's existing helper if one exists — mirror `test_add_then_get_returns_same_shape` at line 83.)

- [ ] **Step 7: Run contract tests (both engines)**

```bash
.venv/bin/pytest tests/db_pg/test_corpus_files_contract.py -q
```

Expected: PASS on DuckDB + PG parametrizations (PG spins up via pixeltable_pgserver; if the PG side is flaky under load, re-run focused).

- [ ] **Step 8: Commit**

```bash
git add src/ingest/tabular.py src/ingest/runner.py src/repositories/corpus_files.py \
        tests/test_ingest_runner.py tests/db_pg/test_corpus_files_contract.py
git commit -m "feat(collections): needs_review status - empty extractions no longer claim indexed"
```

---

### Task 3: Library UI — honest file cards

`app/web/templates/library_detail.html:157` maps pills but knows nothing of `needs_review`, shows no reason, and the `.status-pill` span has **no CSS in this template at all** (renders as bare text). Fix all three.

**Files:**
- Modify: `app/web/templates/library_detail.html` (pill map :157, card body :168-173, CSS block)
- Test: `tests/test_web_library.py` (or wherever `/library/{slug}` template tests live — `grep -rln "library_detail" tests/` first; if none exists, create `tests/test_web_library_detail_status.py` using the app-TestClient fixture pattern from `tests/test_api_collections.py`)

- [ ] **Step 1: Write the failing test**

```python
def test_library_detail_shows_needs_review_reason(client_admin, seeded_collection):
    """File card must badge needs_review and surface the reason text."""
    from src.repositories import corpus_files_repo

    col_id, slug = seeded_collection  # fixture: collection + one file
    files = corpus_files_repo().list_for_corpus(col_id)
    corpus_files_repo().set_status(
        files[0]["id"], status="needs_review",
        detail={"reason": "extraction produced empty table"},
    )
    r = client_admin.get(f"/library/{slug}")
    assert r.status_code == 200
    assert "needs_review" in r.text
    assert "extraction produced empty table" in r.text
```

Adapt fixture names to the actual conventions found in the existing web/API tests (`client_admin` = authenticated admin TestClient; `seeded_collection` = create via `file_corpora_repo().create` + `corpus_files_repo().add` as in `tests/test_ingest_runner.py::_new_corpus/_add_file`). If no suitable fixtures exist, build the collection inline with those two repo calls.

- [ ] **Step 2: Run it — expect FAIL** (reason text absent from HTML)

```bash
.venv/bin/pytest tests/test_web_library_detail_status.py -q
```

- [ ] **Step 3: Implement the template**

`library_detail.html:157` — extend the map:

```jinja
{% set pill = {"indexed": "ok", "processing": "pending", "pending": "pending",
               "rejected": "blocked", "needs_review": "warn"}.get(f.processing_status, "pending") %}
{% set reason = (f.processing_detail or {}).get("reason") %}
```

In the card body (`:168-171`), after `.file__meta`:

```jinja
            {% if reason and f.processing_status in ("rejected", "needs_review") %}
              <div class="file__reason">{{ reason }}</div>
            {% endif %}
```

In the `{% block head_extra %}` CSS (next to `.file__meta`, ~line 93) add pill + reason styles — ds tokens only, no raw hex (design-contract test rejects it):

```css
  .file__reason { font-size: 0.76rem; color: var(--ds-warn-ink); margin-top: 2px; }
  .status-pill {
    flex: none; font-size: 0.72rem; font-weight: var(--font-medium);
    padding: 2px 10px; border-radius: var(--radius-full);
    color: var(--ds-text-secondary);
    background: color-mix(in srgb, var(--ds-text-secondary) 10%, var(--surface));
  }
  .status-pill.ok      { color: var(--ds-success-ink, var(--ds-text-primary)); background: var(--ds-success-bg, var(--surface)); }
  .status-pill.warn    { color: var(--ds-warn-ink); background: var(--ds-warn-bg); }
  .status-pill.blocked { color: var(--ds-danger-ink, var(--ds-text-primary)); background: var(--ds-danger-bg, var(--surface)); }
```

First check the real token names: `grep -o 'ds-[a-z-]*' app/web/static/css/*.css | sort -u` (the template already uses `--ds-warn-ink`/`--ds-warn-bg` at `.result__confidence--low`, so those two are safe; use the actual success/danger token names found, and drop the fallbacks if the tokens exist).

- [ ] **Step 4: Run test + design-contract guard — expect PASS**

```bash
.venv/bin/pytest tests/test_web_library_detail_status.py tests/test_design_system_contract.py -q
```

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/library_detail.html tests/test_web_library_detail_status.py
git commit -m "feat(library): badge needs_review/rejected file cards with the failure reason"
```

---

### Task 4: Re-ingest endpoint

`POST /api/collections/{collection_id}/files/{file_id}/reingest` — purge the file's derived artifacts, reset to `pending`, re-run `ingest_file` in the background. Same grant gate as upload (`require_resource_access(ResourceType.COLLECTION, "{collection_id}")` — `app/api/collections.py:377`).

**Files:**
- Modify: `app/api/collections.py` (new handler after the upload/delete file block)
- Test: `tests/test_api_collections.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_collections.py`, reusing that module's existing client/auth fixtures (mirror the nearby upload/delete-file tests' fixture names exactly):

```python
def test_reingest_resets_status_and_reruns(client_admin, tmp_path):
    """needs_review file + fixed content → reingest → indexed."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ri", slug="ri", description=None, created_by="u1")
    csv = tmp_path / "d.csv"
    csv.write_text("a,b\n", encoding="utf-8")  # header-only → needs_review
    fid = corpus_files_repo().add(
        corpus_id=col_id, filename="d.csv", sha256="s", file_type="csv",
        size_bytes=csv.stat().st_size, storage_path=str(csv),
    )
    from src.ingest.runner import ingest_file
    assert ingest_file(fid) == "needs_review"

    csv.write_text("a,b\n1,2\n", encoding="utf-8")  # operator fixes the file
    r = client_admin.post(f"/api/collections/{col_id}/files/{fid}/reingest")
    assert r.status_code == 202
    assert r.json()["processing_status"] == "pending"

    # TestClient runs BackgroundTasks on response — by now ingest re-ran.
    assert corpus_files_repo().get(fid)["processing_status"] == "indexed"


def test_reingest_404_on_foreign_file(client_admin):
    from src.repositories import file_corpora_repo

    col_a = file_corpora_repo().create(name="ria", slug="ria", description=None, created_by="u1")
    r = client_admin.post(f"/api/collections/{col_a}/files/cf_nonexistent/reingest")
    assert r.status_code == 404
```

- [ ] **Step 2: Run — expect FAIL** (405/404: route missing)

```bash
.venv/bin/pytest tests/test_api_collections.py -q -k reingest
```

- [ ] **Step 3: Implement the handler**

In `app/api/collections.py`, after the file-delete handler:

```python
@router.post("/{collection_id}/files/{file_id}/reingest", status_code=202)
async def reingest_file(
    collection_id: str,
    file_id: str,
    background_tasks: BackgroundTasks,
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{collection_id}")),
):
    """Re-run ingestion for one file (after a fix, a new extractor, or a
    pre-status-honesty backfill).

    Purges the file's derived artifacts first — the derived table_registry
    row/parquet for tabular files (chunks are cleared by the ingest itself,
    which is idempotent) — then resets the row to ``pending`` and re-runs
    ``ingest_file`` in the background. Returns 202 with the pending row.
    """
    cf_repo = corpus_files_repo()
    row = cf_repo.get(file_id)
    if not row or row.get("corpus_id") != collection_id:
        raise HTTPException(status_code=404, detail="file_not_found")

    _purge_derived_tabular_row_for_file(collection_id, file_id)
    cf_repo.set_status(file_id, status="pending", detail={"reason": "reingest requested"})

    from src.ingest.runner import ingest_file
    background_tasks.add_task(ingest_file, file_id)
    return {**_file_out(cf_repo.get(file_id))}
```

Check the module for the actual file-serializer helper (`grep -n "_file_out\|def _file" app/api/collections.py`); if none exists, return the explicit dict the upload handler builds (`file_id`, `filename`, `processing_status`, …) — match its shape. Also confirm the repo row key is `corpus_id` (`grep -n "corpus_id" src/repositories/corpus_files.py`).

- [ ] **Step 4: Run — expect PASS**

```bash
.venv/bin/pytest tests/test_api_collections.py -q
```

- [ ] **Step 5: Re-ingest button on the file card (admin only)**

Extend the Task 3 web test file with:

```python
def test_library_detail_admin_sees_reingest_button(client_admin, seeded_collection):
    col_id, slug = seeded_collection
    r = client_admin.get(f"/library/{slug}")
    assert 'data-reingest' in r.text
```

Run it (expect FAIL), then in `app/web/templates/library_detail.html` after the status pill (`:172`):

```jinja
          {% if is_admin and f.processing_status in ("rejected", "needs_review", "indexed") %}
            {{ ds.button("Re-ingest", variant="ghost", size="sm",
                         attrs='type="button" data-reingest="' ~ f.id ~ '"') }}
          {% endif %}
```

(`is_admin` is already in the context — `app/web/router.py:1430`. Check the `ds.button` macro signature at `app/web/templates/_components.html:44` for the real `size` param name; drop it if the macro has none.)

In the template's existing `<script>` IIFE (it already has `cid` = collection id):

```javascript
      document.querySelectorAll("[data-reingest]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          btn.disabled = true;
          fetch("/api/collections/" + cid + "/files/" + btn.getAttribute("data-reingest") + "/reingest",
                { method: "POST" })
            .then(function (r) { if (r.ok) window.location.reload(); else btn.disabled = false; });
        });
      });
```

Note: the API router uses cookie/session auth for browser calls the same way the existing upload JS in this template does — copy whatever headers/credentials the upload `fetch` passes.

Re-run: `.venv/bin/pytest tests/test_web_library_detail_status.py -q` — expect PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/collections.py app/web/templates/library_detail.html \
        tests/test_api_collections.py tests/test_web_library_detail_status.py
git commit -m "feat(collections): reingest endpoint + Library re-ingest button"
```

---

### Task 5: CLI + MCP surfaces + triple-surface cohort

New endpoint ⇒ must be reachable via CLI and MCP (ratchet: `tests/test_documentation_api_triple_surface.py` `_COHORT`).

**Files:**
- Modify: `cli/commands/collections.py` (new `reingest` command after `upload`, `cli/commands/collections.py:176`)
- Modify: `cli/mcp/server.py` (new tool next to `collections_search`, `cli/mcp/server.py:135`)
- Modify: `tests/test_documentation_api_triple_surface.py:29` (cohort entry)
- Test: `tests/test_cli_collections.py`

- [ ] **Step 1: Write the failing CLI test**

Append to `tests/test_cli_collections.py`, copying the module's existing runner/HTTP-mock pattern (look at how `upload`/`rm` tests stub `api_post_json` or the HTTP layer — mirror it exactly):

```python
def test_collections_reingest_posts_to_endpoint(monkeypatch):
    calls = {}

    def fake_post(path, payload):
        calls["path"] = path
        return {"file_id": "cf_1", "processing_status": "pending"}

    monkeypatch.setattr("cli.commands.collections.api_post_json", fake_post)
    result = runner.invoke(app, ["collections", "reingest", "col_1", "cf_1"])
    assert result.exit_code == 0
    assert calls["path"] == "/api/collections/col_1/files/cf_1/reingest"
    assert "pending" in result.output
```

(If the module imports the client differently — e.g. `from cli.v2_client import api_post_json` inside the command — patch the name the command actually resolves.)

- [ ] **Step 2: Run — expect FAIL** (`No such command 'reingest'`)

```bash
.venv/bin/pytest tests/test_cli_collections.py -q -k reingest
```

- [ ] **Step 3: Implement CLI command**

In `cli/commands/collections.py` (imports at top already pull the v2 client — extend to include `api_post_json` if absent):

```python
@collections_app.command("reingest")
def reingest_file(
    collection_id: str = typer.Argument(..., help="Collection id (col_...)"),
    file_id: str = typer.Argument(..., help="File id (cf_...) from `collections show`"),
):
    """Re-run ingestion for one file (admin; after fixing the file or config)."""
    out = api_post_json(
        f"/api/collections/{collection_id}/files/{file_id}/reingest", {}
    )
    typer.echo(
        f"reingest queued: {out.get('file_id', file_id)} "
        f"status={out.get('processing_status', '?')}"
    )
```

Match the module's real error-handling convention (the sibling commands wrap `V2ClientError` — copy that wrapper).

- [ ] **Step 4: Implement MCP tool**

In `cli/mcp/server.py`, after `collections_search`:

```python
@mcp.tool()
def collections_reingest(collection_id: str, file_id: str) -> dict:
    """Re-run ingestion for one file in a Collection (admin-gated).

    Use after the file or extraction config was fixed — e.g. a file stuck
    in ``needs_review`` (empty extraction) or ``rejected``. Returns the file
    row reset to ``pending``; ingestion runs server-side in the background.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
        file_id: File id from ``collection_get`` (``cf_...``).
    """
    try:
        return api_post_json(
            f"/api/collections/{collection_id}/files/{file_id}/reingest", {}
        )
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_reingest", exc)) from exc
```

(Confirm `api_post_json` is already imported in `cli/mcp/server.py` — `grep -n api_post_json cli/mcp/server.py`; add to the import if missing.)

- [ ] **Step 5: Register in the triple-surface cohort**

`tests/test_documentation_api_triple_surface.py` `_COHORT` dict — add:

```python
    # Collections re-ingest (status-honesty, spec 2026-07-08).
    "/api/collections/{collection_id}/files/{file_id}/reingest": (
        "collections reingest", "collections_reingest"),
```

- [ ] **Step 6: Run the three surfaces' tests — expect PASS**

```bash
.venv/bin/pytest tests/test_cli_collections.py tests/test_documentation_api_triple_surface.py \
                 tests/test_mcp_tools_generator.py -q
```

- [ ] **Step 7: Commit**

```bash
git add cli/commands/collections.py cli/mcp/server.py \
        tests/test_cli_collections.py tests/test_documentation_api_triple_surface.py
git commit -m "feat(cli,mcp): collections reingest command + tool (triple-surface)"
```

---

### Task 6: CHANGELOG + full suite

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`)

- [ ] **Step 1: CHANGELOG bullets**

Under `## [Unreleased]`:

```markdown
### Added
- Collections: `POST /api/collections/{id}/files/{file_id}/reingest` (+ `agnes collections reingest`, MCP `collections_reingest`) — re-run ingestion for one file after a fix.

### Changed
- Collections ingestion honesty: extractions that produce an empty table or zero text chunks now land in a new `needs_review` status (with the reason shown on the Library file card) instead of being marked `indexed`; empty derived tables are no longer registered.

### Fixed
- PDF ingestion works on default installs: `pypdf` is now a core dependency (previously every PDF was rejected unless the heavy `docling` extra was installed).
```

(Merge into existing Added/Changed/Fixed sections if present — don't duplicate headers.)

- [ ] **Step 2: Full suite (CI-equivalent) — detached so it survives the session**

```bash
nohup .venv/bin/pytest tests/ --tb=short -n auto -q > /tmp/pytest-parta.log 2>&1 &
```

Then poll `tail -5 /tmp/pytest-parta.log`. Expected: all green. Failures in touched code: fix before proceeding. Unrelated failures: `git stash` → confirm they reproduce on clean branch → note in PR body.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for ingestion status honesty + reingest"
```

---

## Post-plan (not tasks — process reminders)

- PR against `main`; title `fix(collections): honest ingestion statuses + reingest + core pypdf`. Vendor-agnostic scan of diff + PR body (no customer names/instances).
- Mandatory review loop: `/agnes-review` → fix → Devin Review → resolve threads → repeat until clean; watch `gh pr checks` + post-merge `release.yml` smoke.
- Release-cut decision (agnes-releaser, phase 1) before merge — patch bump per policy.
- Part B (LLM-first ingestion) gets its own plan after this ships — it builds on `needs_review` + the reingest path.

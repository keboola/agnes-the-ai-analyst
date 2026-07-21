# Audit: spotlight onboarding + data upload (rail layout)

**Branch:** `mf/spotlight-onboarding-upload` (from `zs/paper-theme-rail-layout`).
**Scope:** current state of the guided spotlight tour and the "upload data into
Agnes" flow, against the new rail (`ui_layout=rail`) chrome. MVP acceptance spec
was not available at audit time — findings that depend on it are flagged.

## Spotlight onboarding

| # | Severity | Finding | Ref |
|---|---|---|---|
| 1 | High | Tour steps `marketplace` + `memory` anchor on `nav-marketplace` / `nav-memory`, which exist only in `_app_header.html`. The rail folds those under **Catalog** and has no such anchors, so in rail mode both steps silently drop — an incomplete tour on the layout the VM runs. | `app/web/onboarding.py`, `app/web/templates/_app_rail.html` |
| 2 | Med | The contract test matched anchors against a merged blob of **all** templates, so the rail gap passed CI (anchors present in the header). Blind to per-layout drift. | `tests/test_onboarding_not_outdated.py` |
| 3 | Med | No tour step covers **My Stack / uploading your own data** — a core MVP capability. Rail exposes `nav-stack` (untoured) and `nav-chat-new`; topnav exposes `nav-library`. | `app/web/onboarding.py` |

### Fixed in this branch
- Added a `layouts` field to `OnboardingStep`; `marketplace` + `memory` are now
  `topnav`-only, and a new **`stack`** step (rail-only, `nav-stack`) introduces
  My Stack + uploads — closing #1 and #3 for rail.
- `steps_for(is_admin, layout)` filters by the active chrome; `_tour.html` passes
  `ui_layout`.
- Contract test is now **layout-aware**: each step's anchor must exist in its
  layout's nav partial (rail → `_app_rail.html`, topnav → `_app_header.html`),
  closing #2.

## Data upload ("upload directly into Agnes")

The flow is **document ingestion (RAG)** via Collections — `app/api/collections.py`,
UI in `stack_unified.html` ("+ New upload" modal) and `library.html`
("+ New collection"). Create → `POST /api/collections` → `POST /{id}/files` →
async index; private uploads surface as catalog "Upload" cards linking `/library/{slug}`.

| # | Severity | Finding | Ref |
|---|---|---|---|
| 4 | **Needs MVP** | Upload = documents for **agent search**, not tabular data → queryable table (no CSV→DuckDB/catalog path). If MVP means "upload data and query it", that path does not exist. | `app/api/collections.py:398` |
| 5 | Med | Image / OCR ingestion (tier2) explicitly **deferred to "Slice 5"** — unfinished. | `collections.py:411` |
| 6 | Bug | The "+ New upload" modal treated a **422 (rejected file)** as success and reloaded onto a broken empty upload — no error shown. | `app/web/templates/stack_unified.html:217` |
| 7 | Med | No ingest-status feedback in the modal: it `reload()`s while files sit `processing_status='pending'`; the user gets no "indexing…/ready" signal. | `stack_unified.html`, `collections.py` |
| 8 | Q | Shared-collection creation on `/library` is admin-gated; private upload via My Stack works for all. Confirm intended MVP permissions. | `library.html:185` |

### Fixed in this branch
- **#6:** the "+ New upload" modal now navigates to the new collection's detail
  page (`/library/<slug>`) on success **and** on 422 — matching the existing
  library upload flow (`library_detail.html`). Rejected files show their status +
  reason + a re-ingest action there, instead of a silent reload onto `/stack`
  that hid what happened. (The collection is kept, consistent with the
  established pattern — not deleted.)

## Not yet addressed (need MVP spec / decision)
- **#4** — tabular data upload (the likely crux). Decide: documents-only (RAG) or
  add CSV/tabular → queryable table.
- **#5** — image/OCR ingestion (Slice 5).
- **#7** — ingest progress/status UX after upload.
- **#8** — who may create/upload (permissions).

## Verify
- `uv run pytest tests/test_onboarding_not_outdated.py -q` (layout-aware anchor check).
- Manual: `ui_layout=rail` local instance → open the tour → confirm the `stack`
  step spotlights My Stack and no step lands on a missing anchor; upload an
  unsupported file via "+ New upload" → clear rejection message, no orphan card.

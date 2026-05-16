# CLI Flea-Market Upload Workflow

**Date:** 2026-05-16
**Author:** vrysanek
**Status:** Proposal
**Scope:** `cli/commands/flea.py` (or extend `cli/commands/store.py`), `cli/lib/flea_state.py`, one new server endpoint, sidecar file format.
**Related:** PR #290 (hard-reject inline guardrails), PR #295 (post-#290 follow-up), `agnes store {upload,update,delete,mine}` (current creator-side surface).

---

## 1. Context

The current `agnes store` namespace already covers the creator-side
endpoints — `upload`, `update`, `delete`, `mine` — but the workflow is
**ZIP-centric, stateless, and synchronous**:

- The submitter packs their own ZIP and points `agnes store upload` at it.
- The CLI returns once the **inline** checks pass (server-side it's a 201
  response; the async LLM check runs in the background and the result
  lands only on the entity row hours later).
- Re-uploading a new version of the same artifact means manually
  remembering the server-side `entity_id` and switching from
  `upload` to `update --zip ...`.

The user wants a **folder-centric, stateful, async-aware** workflow:

```
# first time
agnes flea push ./my-skill

# anywhere later, from anywhere on disk
agnes flea push ./my-skill   # auto-detects "this is the same artifact"
agnes flea push ./my-skill --watch   # blocks until LLM verdict lands
agnes flea status ./my-skill         # cheap one-shot status check
```

What's missing today:

| Need | Current state |
|---|---|
| Pack a local folder into the upload ZIP automatically | Manual `zip -r` outside the CLI |
| Remember `(folder → server entity_id)` mapping per Agnes instance | No persistence at all |
| Decide POST vs PUT based on prior history | Submitter chooses `upload` or `update` by hand |
| Surface async LLM verdict to the submitter | No submitter-facing endpoint — only admins see findings |
| Block / poll until LLM verdict is final | No CLI affordance |
| Detect "nothing changed since last push, skip" | Always re-uploads |

This proposal fills those gaps. PR #290 already shipped the server-side
two-tier reject model (`validation_failed` / `security_blocked` /
`pending_llm` → `approved`|`blocked_llm`|`review_error`), so the CLI
side just needs to consume what's there + one new endpoint for
submitter-facing submission status.

---

## 2. Goals & non-goals

### Goals

1. **One-command publish loop** — `agnes flea push <folder>` packs, uploads,
   distinguishes create-vs-update, and surfaces async verdict.
2. **Folder-as-identity** — the local folder remembers what it became on
   the server. Multi-instance ready (uploaded the same folder to two
   different Agnes instances? Both mappings survive.)
3. **Polite default** — `flea push` returns immediately after the inline
   pass; users opt in to `--watch` if they want to block on the LLM
   verdict.
4. **Clean separation of concerns** — pack logic, state logic, and
   transport are independent modules so each is unit-testable.
5. **Zero-config for the common case** — type auto-detected from the
   folder contents (`SKILL.md` → skill, `*.md` w/ frontmatter → agent,
   `.claude-plugin/plugin.json` → plugin).
6. **Idempotent re-push** — if the bundle hash hasn't changed since the
   last successful push, skip the upload and just refresh status.

### Non-goals

- Replacing `agnes store {upload,update,delete}` — those stay as the
  low-level ZIP-and-id-driven primitives. `agnes flea` is a higher-level
  wrapper that calls them.
- Editing the inline / LLM check pipeline. Server-side guardrails stay
  exactly as PR #290 shipped them.
- Implementing a generic "agnes config" file format. Sidecar state is
  flea-market specific; reuse of the convention for other features is
  a future concern.
- Migrating existing analyst-uploaded ZIPs. Anyone who used
  `agnes store upload` before this lands keeps using it; `flea push` is
  additive.

---

## 3. User-facing surface

New subcommand namespace `agnes flea`, registered alongside the existing
`store`, `my-stack`, and `marketplace` Typer sub-apps.

Choice of name: `flea` matches the existing internal terminology
("flea market"); keeps `store` reserved for the low-level primitives.
Alternative considered: extend `store` with `store push`. Rejected
because the verb / behavior mismatch with existing `store upload` is
confusing — `push` would silently change between `POST` and `PUT`
depending on hidden state, while `upload` is always `POST`.

### 3.1 `agnes flea push <folder>`

```
agnes flea push <folder> [--watch] [--timeout SECS] [--type TYPE]
                          [--name NAME] [--description TEXT]
                          [--category CAT] [--video-url URL]
                          [--photo IMG] [--force-new] [--dry-run]
```

Behavior:

1. **Detect type** — walks `<folder>` looking for the type markers
   (skill / agent / plugin). Fail fast with hint if ambiguous. Override
   with `--type`.
2. **Pack** — creates `<tmpdir>/<folder-name>.zip` containing the folder
   contents (canonical mtime / ordering so the hash is deterministic).
3. **Hash** — SHA256 of the deterministic ZIP, compared against the
   `last_bundle_sha256` in the sidecar. If equal AND prior submission
   is in a terminal-good state (`approved`), exit with
   `up-to-date, status=approved`.
4. **Look up** — read `<folder>/.agnes-flea.json` (see §4). If a mapping
   exists for the current `instance_url`:
   - `last_status` is non-archived → **update** (PUT).
   - `last_status='archived'` or `archived_at` is set → fall back to
     **create** (POST). PUT to an archived entity is refused
     server-side; the CLI doesn't even try. The old `entity_id`
     migrates into `history[]` and the new one becomes current.
   - `last_status='deleted'` (hard-deleted out of band) → same: POST,
     migrate to history.
   Otherwise (no mapping) → **create** (POST). `--force-new` short-circuits
   the lookup and forces POST regardless.
5. **Upload** — calls `api_post_multipart` or `api_put_multipart` via
   the existing v2 client.
6. **Persist** — on 2xx, writes `entity_id`, `submission_id`,
   `last_bundle_sha256`, `last_uploaded_at`, `name`, `type` into the
   sidecar under the `instance_url` key.
7. **Surface verdict** — by default prints inline-pass + `pending_llm`
   queued; exit 0. With `--watch`, polls the new
   `GET /api/store/submissions/<id>` endpoint (see §6) until terminal
   state, prints structured verdict + findings, exits 0 on approved /
   non-zero on blocked.

### 3.2 `agnes flea status <folder>`

```
agnes flea status <folder> [--json]
```

Reads the sidecar, calls the new submission endpoint for the latest
submission_id, prints `{visibility, sub_status, llm_findings_summary}`.
Cheap one-liner — no upload, no pack.

### 3.3 `agnes flea unlink <folder>`

```
agnes flea unlink <folder> [--instance URL]
```

Removes the mapping for one (or all) instances from the sidecar. Useful
when the entity was deleted server-side and a re-push should create new.
Does NOT call the server. Pure local-state edit.

### 3.4 `agnes flea list`

```
agnes flea list [--all] [--json]
```

Lists `(folder, instance, entity_id, last_status, last_uploaded_at)`
across all known sidecars. Walks a per-user index file
(`~/.config/agnes/flea-uploads.json`) maintained alongside the sidecars
(see §4). Useful for "what have I published from this laptop?"

### 3.5 `agnes flea archive <folder>`

```
agnes flea archive <folder> [--instance URL] [--all-instances] [--yes]
```

Soft-archives the entity that this folder published to the current
Agnes instance. Server-side this maps to
`DELETE /api/store/entities/{entity_id}` (no `?hard=true`), which:

- Flips `visibility_status='archived'`.
- Renames the server-side entity to
  `<original-name>__archived__<epoch>` so the `(owner, name)` and
  `<name>-by-<owner_username>` slug slots free up for a fresh upload
  under the same name.
- Keeps the on-disk bundle + existing `user_store_installs` intact so
  users who already installed the plugin keep getting it through
  `marketplace.zip` / `marketplace.git`.

Behavior:

1. Reads sidecar; refuses if no mapping for the chosen instance.
2. Confirms (interactive prompt or `--yes`) — destructive-ish (frees
   the name slot, can't be undone via the CLI; see §3.7).
3. Calls `api_delete(f"/api/store/entities/{entity_id}")`.
4. **Updates sidecar** — the instance entry stays but its `last_status`
   flips to `archived`, and a new field `archived_at` is stamped.
   The `entity_id` is preserved so `flea status` can still surface
   "this folder was archived as ent_abc123 on 2026-05-17".
5. Next `agnes flea push <folder>` on the same instance creates a NEW
   entity (POST), because PUT to an archived entity_id is refused
   server-side. Sidecar's old `entity_id` is replaced on success.

Server preconditions enforced for the CLI to relay clearly:

| Server reject | CLI behavior |
|---|---|
| 404 `entity_not_found` (already hard-deleted by admin) | Treat as success: update sidecar `last_status='deleted'`, exit 0 with note |
| 403 `not_owner` | Print "you don't own this entity on this instance — owner-only operation" |
| 403 `quarantined_owner_cannot_delete` | Print the server hint verbatim ("under quarantine while admins review") |
| 403 with `hard_delete_admin_only` | Cannot happen — CLI never passes `hard=true` on the `archive` subcommand |

`--all-instances`: archives the entity on every instance the folder
has been pushed to. Useful when retiring a skill globally.

### 3.6 `agnes flea delete <folder>` (admin-only hard delete)

```
agnes flea delete <folder> [--instance URL] [--yes-i-mean-it]
```

Wraps `DELETE /api/store/entities/{entity_id}?hard=true`. **Admin-only**
server-side. The CLI:

1. Refuses without the explicit `--yes-i-mean-it` flag.
2. Surfaces the server's `403 hard_delete_admin_only` cleanly for
   non-admin callers (the same payload that the UI shows).
3. On success, sidecar instance entry is removed entirely (not just
   marked archived) — the bytes are gone server-side and the
   `entity_id` is no longer valid for anything.

This is intentionally distinct from `archive` so the destructive
variant requires deliberate intent. Most submitter workflows should
use `archive`; `delete` is the legal/privacy escape hatch.

### 3.7 `agnes flea diff <folder>`

```
agnes flea diff <folder> [--instance URL]
```

Compares the local folder against the on-server version (downloads from
`GET /api/store/entities/{id}/files` per existing endpoint, diffs).
Stretch goal — phase 2.

---

## 4. Local state

### 4.1 Sidecar file `<folder>/.agnes-flea.json`

```json
{
  "schema_version": 1,
  "instances": {
    "https://agnes.example.com": {
      "entity_id": "ent_abc123",
      "type": "skill",
      "name": "my-skill",
      "submission_id": "sub_xyz789",
      "last_bundle_sha256": "abc...123",
      "last_uploaded_at": "2026-05-16T12:34:56Z",
      "last_status": "approved",
      "archived_at": null,
      "history": [
        {
          "entity_id": "ent_old111",
          "name": "my-skill",
          "archived_at": "2026-04-02T09:15:00Z",
          "reason": "cli_archive"
        }
      ]
    },
    "https://other-agnes.internal": {
      "entity_id": "ent_def456",
      "type": "skill",
      "name": "my-skill",
      "submission_id": "sub_uvw012",
      "last_bundle_sha256": "abc...123",
      "last_uploaded_at": "2026-05-10T08:21:11Z",
      "last_status": "pending_llm",
      "archived_at": null,
      "history": []
    }
  }
}
```

Fields:

| Field | Type | Notes |
|---|---|---|
| `entity_id` | str | Current server-side entity id. Replaced on every successful POST. |
| `submission_id` | str | Latest submission row; what `flea status` / `--watch` polls. |
| `last_bundle_sha256` | hex str | Bundle-content hash of the last successful upload — drives the "skip if unchanged" short-circuit on re-push. |
| `last_status` | enum | One of `pending_inline | pending_llm | approved | blocked_llm | review_error | overridden | archived | deleted`. Mirrors server `store_submissions.status` plus the entity-level lifecycle states. |
| `archived_at` | ISO timestamp \| null | Stamped when `flea archive` succeeds (or when push detects a server-side archive). `null` while the entity is live. Co-presence with non-archived `last_status` means a third party (admin) re-published under the same id — should never happen normally. |
| `history[]` | list of older entity ids | Every previous entity_id this folder published, paired with its archive timestamp and reason. Lets `flea list --history <folder>` reconstruct the chain across renames / archives. |

**Why a sidecar (not a global file)?**

- Travels with the folder if the user commits it to a repo or copies
  the folder to another machine — pushing from the new location keeps
  the mapping.
- Self-documenting — anyone inspecting the folder can see "this was
  published as X on Y".
- One sidecar per artifact, even when the artifact lives across
  multiple Agnes instances.

**Conventions:**

- Always add `.agnes-flea.json` to the suggested skill's `.gitignore`
  via `agnes init` (one-line follow-up if user opts in). Some users
  WILL want to commit it (sharing the mapping across teammates of the
  same instance), so don't force the ignore.
- Atomic writes: tmpfile + `os.replace` so an interrupted push never
  corrupts the sidecar.
- Schema versioning baked in from day one (`schema_version: 1`).

### 4.2 Global index `~/.config/agnes/flea-uploads.json`

```json
{
  "schema_version": 1,
  "folders": {
    "/Users/v/.../my-skill": [
      "https://agnes.example.com",
      "https://other-agnes.internal"
    ]
  }
}
```

Built incrementally on every successful push. Lets `flea list` answer
"what has this laptop pushed where?" without walking the filesystem.
Pure cache — destroyable, rebuildable from sidecars. Lives next to the
existing `~/.config/agnes/sync_state.json` (same convention).

---

## 5. Pack-and-upload pipeline

### 5.1 Type detection

```
def detect_type(folder: Path) -> str:
    if (folder / ".claude-plugin" / "plugin.json").exists():
        return "plugin"
    if (folder / "SKILL.md").exists():
        return "skill"
    # agent: any *.md (NOT SKILL.md) with name+description frontmatter
    for md in folder.glob("*.md"):
        if md.name == "SKILL.md":
            continue
        fm = parse_frontmatter(md.read_text())
        if fm.get("name") and fm.get("description"):
            return "agent"
    raise CliError("Cannot determine artifact type from folder layout.")
```

Mirrors the server-side `_validate_and_extract_metadata` rules from
`app/api/store.py:578`. Same exact triage. CLI fails fast before any
HTTP call.

### 5.2 Deterministic ZIP packing

The bundle hash must be **reproducible** so we can detect "nothing
changed" without comparing every file. Key constraints:

- Fixed mtime (e.g. `2020-01-01T00:00:00Z`) on every entry.
- Sorted relative paths.
- No `__pycache__/`, `.git/`, `.agnes-flea.json`, `.DS_Store`, etc.
- ZIP store mode (no deflate) — deflate output is dependent on zlib
  version. Slightly bigger upload, but reproducible.

```python
def pack_deterministic(folder: Path, out: Path, excludes: set[str]) -> str:
    """Pack folder → out (ZIP). Return SHA256 hex digest."""
    fixed_ts = (2020, 1, 1, 0, 0, 0)
    h = hashlib.sha256()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if any(part in excludes for part in rel.parts):
                continue
            if not path.is_file():
                continue
            zi = zipfile.ZipInfo(str(rel), fixed_ts)
            data = path.read_bytes()
            zf.writestr(zi, data, compress_type=zipfile.ZIP_STORED)
            h.update(data)
    return h.hexdigest()
```

Excludes default: `__pycache__`, `.git`, `.agnes-flea.json`, `.DS_Store`,
`*.pyc`, `node_modules`, `.venv`. Configurable via
`.agnes-flea-ignore` in the folder (glob lines, same shape as
`.gitignore`).

### 5.3 Transport

Reuses existing helpers:

- **Create:** `api_post_multipart("/api/store/entities", files={...}, data={...})`
- **Update:** `api_put_multipart(f"/api/store/entities/{entity_id}", files={...}, data={...})`

No new transport code required.

### 5.4 Verdict surfacing — non-watch mode

```
$ agnes flea push ./my-skill
flea: packing ./my-skill (skill, 23 files, 412 KB)
flea: bundle sha256 = abc...123
flea: PUT https://agnes.example.com/api/store/entities/ent_abc123
flea: inline checks passed
flea: submission sub_xyz789 queued for LLM review
flea: visibility = pending (LLM verdict pending)

Check status later:
  agnes flea status ./my-skill
  agnes flea push ./my-skill --watch
```

### 5.5 Verdict surfacing — `--watch`

```
$ agnes flea push ./my-skill --watch
flea: packing ... (as above through line 4)
flea: polling submission sub_xyz789 (timeout 600s)...
flea: ⏳ 18s elapsed — status=pending_llm
flea: ⏳ 42s elapsed — status=pending_llm
flea: ✅ status=approved (LLM cleared after 51s)
flea: visibility = approved — live at /marketplace?tab=flea
```

Poll cadence: 2s → 5s → 10s exponential up to 30s max, default
timeout 600s, overridable with `--timeout`. Implemented as a small
`backoff_iter()` helper, unit-testable.

Exit codes:

| Status | Exit |
|---|---|
| `approved` | 0 |
| `pending_llm` after timeout | 0 (warn + URL to status) |
| `blocked_llm` | 2 (with findings summary) |
| `review_error` | 3 |
| HTTP error talking to server | 1 |
| Folder is not in a syncable state | 64 |

---

## 6. Server-side gap fill

### 6.1 New endpoint `GET /api/store/submissions/{submission_id}`

PR #290 left the submitter-facing side of submission status implicit
(the entity's `visibility_status` is exposed, but the LLM
findings + verdict aren't). Add one new endpoint:

```python
@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionStatusResponse,
)
def get_submission_status(
    submission_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> SubmissionStatusResponse:
    """Authenticated submitter reads ONE of their own submissions.

    403 if submitter_id != caller user_id (and caller is not admin).
    Returns status + redacted llm_findings + inline_checks summary.
    """
```

Response model:

```python
class SubmissionStatusResponse(BaseModel):
    id: str
    entity_id: Optional[str]
    type: str
    name: str
    version: str
    status: str  # pending_inline | pending_llm | approved | blocked_llm | review_error | overridden | deleted | archived
    inline_checks: Optional[dict]
    llm_findings_summary: Optional[str]  # human-readable, redacted of probe text
    created_at: str
    updated_at: str
```

**Why redact `llm_findings`?** The full LLM findings include the model's
prompt-injection-attempt detection probe text — surfacing that
verbatim to the submitter would teach attackers how to evade. The
existing `/admin/store/submissions/{id}` endpoint keeps full findings
visible because admins need to act on them. Submitters get a one-line
human summary (`"LLM flagged: prompt-injection probe in
agents/foo.md:12"` style) good enough for self-service iteration.

### 6.2 Auth gate

Use the same pattern as the existing `/store/entities/{id}` GET:

```python
if user["id"] != sub["submitter_id"] and not is_admin(user, conn):
    raise HTTPException(status_code=403, detail="not_your_submission")
```

### 6.3 Audit + rate limit

- No audit log entry on read (cheap, idempotent, called by polling).
- slowapi limit: `60/min` per user (more than enough for `flea push
  --watch` exponential backoff, low enough to bound polling abuse).

---

## 7. Error handling

| Scenario | Server response | CLI behavior |
|---|---|---|
| Bundle layout wrong (pre-bake) | 422 `zip_missing_skill_md` etc. | Print server detail + suggest `agnes flea push --type X` |
| `validation_failed` (inline) | 422 `{code, checks: {manifest, content, quality}}` | Pretty-print which check failed + suggest fix (e.g. "description too short — need ≥60 chars") |
| `security_blocked` (inline) | 422 `{code, checks: {static_security}}` | Print matched rule(s) + URL to security policy doc |
| `blocked_llm` (post-pending) | 422 returned on `flea status` if `--strict`; otherwise structured exit 2 with summary | Same |
| Stale `entity_id` (deleted server-side) | 404 on PUT | Auto-fall-back to POST (create new), migrate old id into `history[]`, sidecar `last_status='deleted'`, print one-line note. No `--force-new` needed. |
| Entity archived server-side (by admin or other channel) | 400 / 409 on PUT | Same auto-fall-back as above — `history[]` gets the old id with `reason="archived_server_side"` |
| Owner mismatch on PUT (entity transferred / orphaned) | 403 `not_owner` | Print server detail; do NOT silently re-create. Suggest `agnes flea unlink` if user really wants to fork off a new entity under their own ownership. |
| Network error | curl-level failure | Retry once, then surface error verbatim — sidecar is NOT updated on failure |
| Empty folder | n/a (caught client-side) | Pre-flight refuse with "no skill / agent / plugin marker found" |
| Folder with `.git/` and no excludes config | Default exclude tuple covers it | Silent, no warning |
| `--watch` interrupted by Ctrl-C | n/a | Print "polling cancelled; status check later with `agnes flea status`" + exit 130 |

---

## 8. Backwards compatibility

| Surface | Impact |
|---|---|
| `agnes store upload <type> <zip>` | **Unchanged.** Still works exactly as today; the ZIP-driven primitive. |
| `agnes store update <id> --zip <zip>` | **Unchanged.** |
| `agnes store mine` | **Unchanged.** |
| `agnes store delete` | **Unchanged.** |
| `agnes marketplace` | **Unchanged.** |
| `agnes my-stack` | **Unchanged.** |
| Existing `POST /api/store/entities`, `PUT /api/store/entities/{id}` | **Unchanged.** |
| New `GET /api/store/submissions/{id}` | **Additive.** Returns 404 from older servers; CLI degrades to "watch on entity visibility_status" with one-line warning. |

The CLI does a one-time capability probe on first `flea push --watch`
(`HEAD /api/store/submissions/healthcheck`) and caches the result in
the sidecar; older servers fall back to polling
`GET /api/store/entities/{id}` and watching `visibility_status` only.

---

## 9. Testing plan

### 9.1 Unit tests

- `pack_deterministic`: same input → same hash, different content →
  different hash, ignore-list honored, fixed mtime on entries.
- `detect_type`: each of skill / agent / plugin / ambiguous cases.
- Sidecar read/write round-trip, schema version handling, multi-instance.
- `backoff_iter`: 2/5/10/20/30/30… cadence, timeout enforcement.

### 9.2 Integration tests (with mocked HTTPX)

- First push → POST, sidecar written.
- Re-push from same folder → PUT to the stored entity_id.
- Re-push with no content changes → no upload, prints "up-to-date".
- `--force-new` → POST even with sidecar present.
- `--watch` + mocked status endpoint → exits 0 on approved, 2 on
  blocked_llm.
- Validation-tier failure (short description SKILL.md) → exits 64 with
  pretty-printed reason.

### 9.3 End-to-end (real HTTPX against test app)

- `tests/test_cli_flea_e2e.py` using the existing `web_client` fixture
  (see `tests/test_admin_store_submissions.py`):
  - Push folder → POST → 201 → verify sidecar populated.
  - Re-push same folder → PUT → 200 → sidecar `last_bundle_sha256`
    matches new content.
  - Re-push unchanged → CLI exits 0 without an upload (HTTP intercept
    asserts no POST/PUT called).
  - Delete entity server-side, re-push → 404 → auto-fall-back to POST,
    old `entity_id` lands in sidecar `history[]`.
  - **Archive lifecycle** — push folder, then `flea archive` → server
    flips visibility to `archived`, sidecar `last_status='archived'`,
    `archived_at` populated. Re-push → creates a NEW entity (POST,
    different `entity_id`), history[] contains the old id.
  - **Archive `--all-instances`** — push to two mocked instances, then
    archive --all-instances → both instance entries flip status; if
    one mock returns 5xx, the other still succeeds.
  - **Hard delete refused for non-admin** — `flea delete
    --yes-i-mean-it` as non-admin returns the server's
    `hard_delete_admin_only` payload; sidecar unchanged.
  - **Hard delete as admin** — sidecar instance entry removed entirely
    (not migrated to history) because the bytes are gone.
  - **Quarantined-entity archive blocked** — admin pushes a bundle that
    lands `blocked_llm`; owner tries `flea archive` → server returns
    `quarantined_owner_cannot_delete` → CLI relays the hint verbatim.

---

## 10. Implementation phases

### Phase 1 — CLI (1-2 days)

- `cli/commands/flea.py` with `push`, `status`, `archive`, `delete`,
  `unlink`, `list`
- `cli/lib/flea_state.py` (sidecar read/write, global index,
  history[] migration on archive/server-archive-detected)
- `cli/lib/flea_pack.py` (deterministic ZIP)
- `cli/lib/flea_types.py` (type detection)
- Register `flea_app` in `cli/main.py`
- Unit + integration tests covering create/update/archive/delete +
  the auto-fall-back-to-POST path when the server reports archived /
  deleted

### Phase 2 — Server endpoint (½ day)

- `GET /api/store/submissions/{id}` with submitter / admin auth gate
- `SubmissionStatusResponse` pydantic model
- LLM findings summarization helper (`_redact_llm_findings(...)`) that
  returns a one-line human summary
- `tests/test_store_submission_status_endpoint.py`

### Phase 3 — Wire `--watch` to new endpoint (½ day)

- CLI probes capability (`HEAD` against the new endpoint)
- Polling loop with exponential backoff
- Pretty-printed verdict

### Phase 4 — Documentation + release-cut

- `docs/HOWTO/publishing-to-flea-market.md` (new analyst-facing doc)
- CHANGELOG entry under `### Added`
- `pyproject.toml` version bump as last commit on the PR (per
  RELEASING.md release-cut rule)

Total: 2-3 days of focused work, splittable across 1-2 PRs (Phase 1 +
Phase 2 in one PR; Phase 3 + Phase 4 in the second if the first lands
fast enough).

---

## 11. Open questions

1. **Sidecar location.** Hidden file (`.agnes-flea.json`) vs visible
   (`agnes-flea.json`)? Hidden matches `.gitignore` / dotfile
   conventions but is less discoverable. **Tentative answer:** hidden;
   `agnes flea status` surfaces the data when needed.

2. **Should `flea push` auto-write `.gitignore` entry?** If the folder
   is in a git repo, auto-add `.agnes-flea.json` to `.gitignore`?
   **Tentative answer:** No by default — surprises users; mention it
   in the post-push hint instead, opt-in flag `--gitignore-sidecar`.

3. **Multiple folders mapping to the same `entity_id`.** Should the
   CLI refuse if `flea push` would PUT to an entity already owned by
   a different local folder? **Tentative answer:** Warn but allow —
   common during folder renames. The owner check is server-side; local
   ambiguity isn't dangerous.

4. **Polling vs server-sent events.** Worth wiring through
   `services/ws_gateway/` for push-based status? **Tentative answer:**
   No — the WS gateway is internal; polling at 2-30s cadence is fine
   for the user-facing latency.

5. **Type override safety.** If folder has both `SKILL.md` AND
   `.claude-plugin/plugin.json`, the server already rejects with
   `zip_looks_like_plugin`. Should the CLI also reject pre-upload?
   **Tentative answer:** Yes — fail fast with a clearer error than
   the wire protocol gives.

6. **Submission status retention.** Server keeps `store_submissions`
   indefinitely. Should `flea push` be allowed to look up an
   `entity_id` whose latest submission row was archived? **Tentative
   answer:** Use the entity's latest submission row regardless of
   archival; that's what `/admin/store/submissions/{id}` already does.

7. **Multi-machine sharing.** If teammate A pushes from folder X on
   machine 1, and teammate B clones the same git repo (which committed
   `.agnes-flea.json`) and pushes from machine 2 — should that
   second push go to the same entity? **Tentative answer:** Yes,
   that's by design. Server-side owner check is the gate; both
   teammates have to be the entity owner or in the Admin group.

8. **Archive vs delete naming.** Most submitter workflows want
   `archive` (free the name slot, keep existing installs working) —
   but the server endpoint is `DELETE /api/store/entities/{id}` with
   `?hard=true` as the destructive variant, so "DELETE = soft" is a
   surprising default if you read the API. **Tentative answer:** The
   CLI shadows the API's defaults with clearer names: `flea archive`
   = the soft default, `flea delete --yes-i-mean-it` = the
   admin-only hard delete. The existing `agnes store delete` keeps
   the API-shaped semantics for backward compat; `flea` is the
   user-friendly layer.

9. **Sidecar archive history retention.** Should `history[]` be capped
   (e.g. last 10 archived entity_ids)? Uncapped lists grow forever
   for a folder pushed-and-archived many times. **Tentative answer:**
   Uncapped for now — entries are small (~80 bytes each); revisit if
   real-world sidecars cross 100 KB.

10. **`flea archive --all-instances` partial failures.** If three
    instances are linked and one returns 5xx, what state does the
    sidecar end up in? **Tentative answer:** Per-instance independent
    commit — successful archives are persisted, failed ones stay
    `last_status='approved'` with an error printed. Caller re-runs
    `flea archive` to retry the failures. Same semantics as
    `agnes pull` partial-table failures today.

---

## 12. Acceptance criteria

- `agnes flea push ./skill-folder` produces an entity on a fresh
  Agnes instance with a populated sidecar.
- Running the same command again, with no content change, prints
  "up-to-date" and makes zero HTTP calls.
- Editing a file in the folder and re-running pushes a new version
  (PUT, not POST) — `entity_id` stays the same, server reflects new
  bundle hash.
- `agnes flea push --watch` blocks until the LLM verdict lands and
  exits with the verdict's expected exit code.
- `agnes flea status ./skill-folder` prints the latest verdict +
  pending count without uploading.
- `agnes flea list` shows every folder this laptop has pushed,
  grouped by instance.
- Validation-tier inline failures produce a clear, fixable error;
  security-tier failures produce a clear, NOT-fixable error pointing
  at the matched static-security rule.
- `agnes flea archive ./skill-folder` flips the server-side
  visibility to `archived` and frees the `(owner, name)` slot;
  re-pushing the same folder afterwards creates a fresh entity
  (POST) and migrates the old `entity_id` into the sidecar
  `history[]`. The old archived entity remains available to users
  who had already installed it.
- `agnes flea delete --yes-i-mean-it` requires admin and is refused
  cleanly (with the `hard_delete_admin_only` server hint) for
  non-admin callers.
- All four phases above ship with green CI on the full pytest suite.

---

## 13. References

- PR #290 — `feat(store): hard-reject inline guardrail failures, trace security only` — established the two-tier reject contract this proposal consumes
- PR #295 — `fix(store-guardrails): post-#290 review follow-up` — locked the `validation_failed` / `security_blocked` JSON contract via the new test
- `app/api/store.py:_reject_inline_or_continue` — the gate that this CLI polls behind
- `app/api/store.py:delete_entity` (line 2336) — soft-archive semantics, name-slot freeing, `?hard=true` admin gate, quarantined-owner refusal — what `flea archive` and `flea delete` ride on
- `cli/commands/store.py` — existing primitives this proposal wraps
- `cli/lib/pull.py:529` — existing per-workspace state convention (`~/.config/agnes/sync_state.json`)
- `docs/STORE_GUARDRAILS.md` — server-side guardrail design (status & findings semantics)

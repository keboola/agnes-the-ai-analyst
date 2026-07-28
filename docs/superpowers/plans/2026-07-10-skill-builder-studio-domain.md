# Skill Builder — Fifth Studio Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `skill` as the fifth authoring-studio domain (issue #688): a guided Skill Builder at `/admin/studio/skill` that publishes a SKILL.md straight into the store's existing guardrail + review pipeline.

**Architecture:** The studio shell (`app/web/studio.py` + `admin_studio.html` + `studio.js`) is generic — a new domain is a `StudioDomain` entry plus a chat profile. The one mismatch: the store's create endpoint is multipart-ZIP, while the studio posts JSON. We add a thin JSON sibling `POST /api/store/entities/from-markdown` that synthesizes `<name>/SKILL.md` into an in-memory ZIP and **delegates to the existing `create_entity`**, so quota, inline guardrails, LLM review, naming and versioning apply identically by construction. Because the store already IS a moderation pipeline (inline checks + async LLM review + admin submissions queue), the skill domain bypasses the `authoring_suggestions` queue: a new `StudioDomain.submit_directly` flag makes everyone (admin and non-admin) post directly to the endpoint, and the suggestions API rejects such domains so nothing can land in a queue that has no replay function.

**Tech Stack:** FastAPI, Pydantic, DuckDB-backed store repos (unchanged), Jinja2 + vanilla-JS studio shell, Typer CLI, FastMCP HTTP tools, Playwright browser E2E.

## Global Constraints

- **No schema migration** — no new tables/columns; profiles are spawn-time-only, the endpoint reuses `store_entities`. Therefore no DuckDB↔PG parity work and no repo-factory changes.
- **Guardrail parity is non-negotiable** — the JSON path MUST route through `create_entity` (never a parallel insert), so `_stream_to_temp` limits, `_NAME_RE`, category normalization, spam quota, content checks and `_schedule_llm_review` all fire.
- **Triple-surface rule** — the new endpoint needs a CLI command (`agnes store publish-md`) and an MCP tool (`store_publish_markdown`), registered in `_COHORT` in `tests/test_documentation_api_triple_surface.py`.
- **Store content floors** (from `src/store_guardrails/content_check.py`, asserted in tests): description ≥ 30 chars and ≥ 4 distinct words; skill body ≥ 200 chars. All test/E2E payloads must clear them.
- **Skill name format:** `^[a-z][a-z0-9-]{0,63}$` (`_NAME_RE` in `app/api/store.py:90`).
- **Vendor-agnostic repo** — no customer names in code, tests, docs, commits, or the PR body.
- **CHANGELOG discipline** — one bullet under `## [Unreleased]` → Added, in the same PR.
- **Full suite before push:** `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- **Out of scope (explicit YAGNI):** the *improver* half of #688 (edit suggestions over an existing store skill — different flow via `PUT /entities/{id}`, follow-up); `references/` multi-file authoring (the JSON contract carries a single SKILL.md; ZIP upload already covers multi-file).

---

### Task 1: JSON publish endpoint — `POST /api/store/entities/from-markdown`

**Files:**
- Modify: `app/api/store.py` (new request model + endpoint next to `create_entity`, which ends ~line 1878)
- Test: `tests/test_store_api.py` (new class, reuses module fixtures `web_client`, `_create_user`, `_OK_DESC`, `_OK_BODY`)

**Interfaces:**
- Consumes: existing `create_entity(background_tasks, file, type, name, description, category, video_url, title, tagline, photo, docs, user, conn)` in the same module; `_NAME_RE`; `StoreEntityResponse`.
- Produces: `POST /api/store/entities/from-markdown` accepting JSON `{"type": "skill" (optional, default), "name": str, "description": str|None, "category": str|None, "skill_md": str}` → 201 `StoreEntityResponse` (same shape as ZIP upload: `id`, `name`, `invocation_name`, `version`, `visibility_status`, …). Tasks 3, 5, 6, 7 all call this exact path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store_api.py`:

```python
class TestCreateFromMarkdown:
    """JSON sibling of POST /entities — the studio Skill Builder path."""

    def _publish(self, client, cookies, **overrides):
        payload = {
            "name": "md-first-skill",
            "description": _OK_DESC,
            "skill_md": _OK_BODY,
        }
        payload.update(overrides)
        return client.post(
            "/api/store/entities/from-markdown", json=payload, cookies=cookies
        )

    def test_publishes_skill_without_frontmatter(self, web_client, tmp_path):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = self._publish(web_client, cookies)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["type"] == "skill"
        assert body["name"] == "md-first-skill"
        assert body["invocation_name"] == "md-first-skill-by-alice"
        # The baked tree exists exactly like a ZIP upload's.
        skill_md = (
            tmp_path / "store" / body["id"] / "plugin" / "skills"
            / "md-first-skill-by-alice" / "SKILL.md"
        )
        assert skill_md.is_file()
        text = skill_md.read_text()
        assert "name: md-first-skill-by-alice" in text  # synthesized + suffixed
        assert _OK_DESC.split()[0] in text  # description landed in frontmatter

    def test_keeps_existing_frontmatter(self, web_client):
        _, cookies = _create_user(web_client, "bob@x.com")
        full = f"---\nname: md-first-skill\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n"
        r = self._publish(web_client, cookies, skill_md=full)
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "md-first-skill"

    def test_rejects_bad_name(self, web_client):
        _, cookies = _create_user(web_client, "carol@x.com")
        r = self._publish(web_client, cookies, name="Bad Name!")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_name_format"

    def test_guardrails_apply_same_as_zip(self, web_client):
        """Short body must be blocked by the content guardrail, proving the
        JSON path rides the same pipeline as the multipart one."""
        _, cookies = _create_user(web_client, "dave@x.com")
        r = self._publish(web_client, cookies, skill_md="too short")
        assert r.status_code == 422, r.text

    def test_rejects_non_skill_type(self, web_client):
        _, cookies = _create_user(web_client, "erin@x.com")
        r = self._publish(web_client, cookies, type="plugin")
        assert r.status_code == 422  # pydantic Literal["skill"]

    def test_requires_auth(self, web_client):
        r = web_client.post(
            "/api/store/entities/from-markdown",
            json={"name": "x", "skill_md": "y"},
        )
        assert r.status_code in (401, 403)
```

Note on `test_guardrails_apply_same_as_zip`: the inline content check rejects a sub-200-char skill body with a 422 (same tier as `zip_invalid`/content issues — see `tests/test_store_guardrails_content.py` for the canonical assertion shape; mirror whatever status+detail shape those tests assert if 422 alone is too loose).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_store_api.py::TestCreateFromMarkdown -q`
Expected: FAIL — all with 404 (route does not exist); `test_requires_auth` may already pass (404 vs 401) — tighten later if so.

- [ ] **Step 3: Implement the endpoint**

In `app/api/store.py`, add `Literal` to the `typing` import (line 32), then insert directly **above** `@router.post("/entities", …)` (~line 1590):

```python
class CreateFromMarkdownBody(BaseModel):
    """JSON contract for markdown-first publishing (studio Skill Builder)."""

    type: Literal["skill"] = "skill"
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    skill_md: str


@router.post(
    "/entities/from-markdown",
    response_model=StoreEntityResponse,
    status_code=201,
)
async def create_entity_from_markdown(
    body: CreateFromMarkdownBody,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """JSON sibling of ``POST /entities`` — synthesizes ``<name>/SKILL.md``
    into an in-memory ZIP and delegates to ``create_entity``, so quota,
    guardrails, LLM review, naming and versioning apply identically.
    """
    name = body.name.strip()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid_name_format")
    text = body.skill_md
    if not _FRONTMATTER_RE.match(text.lstrip()):
        import yaml

        fm = yaml.safe_dump(
            {"name": name, "description": body.description or ""},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        text = f"---\n{fm}---\n\n{text}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", text)
    buf.seek(0)
    upload = UploadFile(file=buf, filename=f"{name}.zip")
    return await create_entity(
        background_tasks,
        file=upload,
        type=body.type,
        name=name,
        description=body.description,
        category=body.category,
        video_url=None,
        title=None,
        tagline=None,
        photo=None,
        docs=[],
        user=user,
        conn=conn,
    )
```

Notes for the implementer:
- `io`, `zipfile`, `UploadFile`, `BaseModel`, `_FRONTMATTER_RE` (line 91) are already imported/defined in this module — only `Literal` is new.
- `_FRONTMATTER_RE` matches at string start; the `.lstrip()` tolerates leading whitespace the studio textarea may introduce.
- Frontmatter synthesis via `yaml.safe_dump` is round-trip-safe: `src/store_guardrails/_frontmatter.py` parses with `yaml.safe_load` first.
- Delegation must stay a plain `await create_entity(...)` call — do NOT copy any pipeline logic into this function.
- FastAPI route ordering: `/entities/from-markdown` vs `/entities/{entity_id}` — literal segments win over path params in FastAPI matching, but keep the new route registered before `get_entity` anyway (it is, if inserted above `create_entity`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_store_api.py::TestCreateFromMarkdown tests/test_store_api.py -q`
Expected: new class PASS, rest of the store suite unchanged (no regressions).

- [ ] **Step 5: Commit**

```bash
git add app/api/store.py tests/test_store_api.py
git commit -m "feat(store): JSON publish endpoint POST /entities/from-markdown"
```

---

### Task 2: `skill-author` chat profile

**Files:**
- Modify: `app/chat/profiles.py` (new `ChatProfile` + `_PROFILES` entry)
- Test: `tests/test_chat_profiles.py` if it exists, else the module test that covers `get_profile` — locate with `grep -rln "get_profile" tests/`; if none covers profiles directly, create `tests/test_chat_profiles.py`

**Interfaces:**
- Consumes: `ChatProfile` dataclass, `_PROFILES` dict (both in `app/chat/profiles.py`).
- Produces: `get_profile("skill-author")` returns a profile whose `skill_name == "agnes-skill-authoring"`. Task 3 references the slug `skill-author` in `StudioDomain.profile`.

- [ ] **Step 1: Write the failing test**

```python
def test_skill_author_profile_registered():
    from app.chat.profiles import get_profile

    p = get_profile("skill-author")
    assert p is not None
    assert p.skill_name == "agnes-skill-authoring"
    assert "use when" in p.claude_md.lower()  # trigger-quality rule is in the persona
    assert p.skill_body.startswith("---\n")
    assert "/api/store/entities/from-markdown" in p.skill_body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chat_profiles.py -q` (adjust filename to where the test landed)
Expected: FAIL — `assert p is not None` (unknown slug).

- [ ] **Step 3: Add the profile**

In `app/chat/profiles.py`, after `_CORPORATE_MEMORY` (line 163):

```python
_SKILL_AUTHOR = ChatProfile(
    slug="skill-author",
    claude_md=(
        "# Skill Builder\n\n"
        "You help a user author a **reusable skill** — a SKILL.md that the "
        "store reviews and distributes to analysts' AI harnesses.\n\n"
        "Rules:\n"
        "- Check the store for near-duplicates first; suggest improving an "
        "existing skill instead if one already covers the need.\n"
        "- The frontmatter `description` must encode a clear *'use when …'* "
        "trigger — that is how an agent decides to load the skill.\n"
        "- Keep the body focused and under ~5k tokens; skills are instructions, "
        "not documentation dumps.\n"
        "- Skills are plain Markdown — write them harness-agnostic, never "
        "assuming one specific AI product.\n"
        "- Draft into the builder fields; never claim the skill is published "
        "until the user clicks Create.\n"
        "- Use the `agnes-skill-authoring` skill for the contract and endpoints.\n"
    ),
    skill_name="agnes-skill-authoring",
    skill_body=(
        "---\n"
        "name: agnes-skill-authoring\n"
        "description: How skills work in Agnes — the SKILL.md contract, the "
        "store review pipeline that distributes them, and the publish endpoints.\n"
        "---\n\n"
        "# Skills in Agnes\n\n"
        "A skill = a folder with `SKILL.md` (YAML frontmatter `name` + "
        "`description`, then Markdown instructions), stored as a "
        "`store_entities` row and served to analysts through the aggregated "
        "marketplace.\n\n"
        "## Contract\n"
        "- `name`: lowercase letters, digits, dashes (`^[a-z][a-z0-9-]{0,63}$`).\n"
        "- `description`: one line encoding the *use when …* trigger "
        "(>= 30 chars, >= 4 distinct words).\n"
        "- Body: >= 200 chars of instructions; keep it under ~5k tokens.\n\n"
        "## Publish\n"
        "- `POST /api/store/entities/from-markdown` — JSON `{type: 'skill', "
        "name, description, category, skill_md}`; the server wraps it into "
        "the same guardrail + review pipeline as ZIP uploads.\n"
        "- `POST /api/store/entities/dryrun` — validate a full ZIP before "
        "publishing (multi-file skills with `references/`).\n"
        "- Uploads may be held for automated review "
        "(`visibility_status: pending`) before appearing in the marketplace.\n"
    ),
)
```

And register it in `_PROFILES` (line 165):

```python
_PROFILES: dict[str, ChatProfile] = {
    _DATA_PACKAGE_BUILDER.slug: _DATA_PACKAGE_BUILDER,
    _MCP_CONNECT.slug: _MCP_CONNECT,
    _MARKETPLACE_AUTHOR.slug: _MARKETPLACE_AUTHOR,
    _CORPORATE_MEMORY.slug: _CORPORATE_MEMORY,
    _SKILL_AUTHOR.slug: _SKILL_AUTHOR,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_chat_profiles.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/chat/profiles.py tests/test_chat_profiles.py
git commit -m "feat(studio): skill-author chat profile"
```

---

### Task 3: `skill` domain registration + `submit_directly` flag + suggestions guard

**Files:**
- Modify: `app/web/studio.py` (dataclass field + domain entry)
- Modify: `app/api/authoring_suggestions.py:146-162` (`submit_suggestion` guard)
- Test: `tests/test_authoring_suggestions_api.py`, `tests/test_web_studio.py` (route smoke lands in Task 4)

**Interfaces:**
- Consumes: Task 1's endpoint path, Task 2's profile slug; `StudioDomain`/`StudioField` dataclasses; `STORE_CATEGORIES` from `src/store_categories.py`.
- Produces: `get_domain("skill")` → `StudioDomain(submit_directly=True, endpoint="/api/store/entities/from-markdown", profile="skill-author", fields with keys name/description/category/skill_md)`. `StudioDomain.submit_directly: bool` (default `False`) — Task 4's template/JS reads it. `POST /api/studio/suggestions` with `domain="skill"` → 400 `{"kind": "domain_submits_directly"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authoring_suggestions_api.py`:

```python
def test_submit_rejects_direct_domain(seeded_app):
    """Domains with their own moderation (the store) must not enter the
    suggestions queue — there is no _SAFE_REPLAY for them, so an approve
    would silently create nothing."""
    c = seeded_app["client"]
    r = _submit(
        c,
        seeded_app["analyst_token"],
        domain="skill",
        payload={"name": "x", "skill_md": "y"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "domain_submits_directly"
```

And a unit check for the registry — append to `tests/test_web_studio.py`:

```python
def test_skill_domain_registered_as_direct_submit():
    from app.web.studio import STUDIO_DOMAINS, get_domain

    spec = get_domain("skill")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "skill-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]
    # every other domain still routes through the suggestions queue
    assert all(
        not d.submit_directly for s, d in STUDIO_DOMAINS.items() if s != "skill"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_authoring_suggestions_api.py::test_submit_rejects_direct_domain tests/test_web_studio.py::test_skill_domain_registered_as_direct_submit -q`
Expected: FAIL — suggestions test gets 201 or `unknown_domain` 400 with the wrong `kind`; registry test gets `spec is None`.

- [ ] **Step 3: Implement**

In `app/web/studio.py` — add the flag to the dataclass (after `endpoint`, line ~31):

```python
    endpoint: str  # admin endpoint the Create action POSTs to
    # True → the domain has its own moderation pipeline (e.g. the store's
    # guardrail + LLM review); EVERYONE posts directly to `endpoint` and the
    # authoring_suggestions queue rejects it (no _SAFE_REPLAY exists).
    submit_directly: bool = False
    fields: tuple[StudioField, ...] = field(default_factory=tuple)
```

(Keep `fields` last — it has a `default_factory`; `submit_directly` must come before it or dataclass ordering breaks. Both have defaults, so either order compiles — match the shown order for readability.)

Add the import at the top of the module:

```python
from src.store_categories import STORE_CATEGORIES
```

Add the domain entry to `STUDIO_DOMAINS` after `"corporate-memory"`:

```python
    "skill": StudioDomain(
        slug="skill",
        profile="skill-author",
        title="Skill Builder",
        subtitle="Author a reusable skill and publish it to the store.",
        endpoint="/api/store/entities/from-markdown",
        submit_directly=True,
        fields=(
            StudioField(
                "name",
                "Name",
                required=True,
                placeholder="quarterly-report-recipe",
            ),
            StudioField(
                "description",
                "Description",
                type="textarea",
                required=True,
                placeholder="Use when … (the trigger that tells an agent to load this skill).",
            ),
            StudioField(
                "category",
                "Category",
                type="select",
                options=tuple(["", *STORE_CATEGORIES]),
            ),
            StudioField(
                "skill_md",
                "Skill content (Markdown)",
                type="textarea",
                required=True,
                placeholder="Step-by-step instructions an AI agent should follow…",
            ),
        ),
    ),
```

(The leading `""` option keeps category genuinely optional — `collectPayload()` in `studio.js` skips empty values. Check `StudioField`'s actual constructor signature at the top of `app/web/studio.py` for the `options`/`type` kwarg names and mirror the existing `mcp` transport field.)

In `app/api/authoring_suggestions.py`, replace the domain check in `submit_suggestion` (lines 151-152):

```python
    spec = get_domain(body.domain)
    if spec is None:
        raise HTTPException(status_code=400, detail={"kind": "unknown_domain", "hint": body.domain})
    if spec.submit_directly:
        raise HTTPException(
            status_code=400,
            detail={"kind": "domain_submits_directly", "hint": spec.endpoint},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_authoring_suggestions_api.py tests/test_web_studio.py -q`
Expected: PASS, including all pre-existing tests (the 4-domain parametrizations are untouched).

- [ ] **Step 5: Commit**

```bash
git add app/web/studio.py app/api/authoring_suggestions.py tests/test_authoring_suggestions_api.py tests/test_web_studio.py
git commit -m "feat(studio): register skill as fifth domain with direct-submit flow"
```

---

### Task 4: Studio shell — template copy, `studio.js` role logic, command palette

**Files:**
- Modify: `app/web/templates/admin_studio.html` (footer note + button label + `window.STUDIO`)
- Modify: `app/web/static/js/studio.js` (`createEntity` role branch)
- Modify: `app/web/templates/_app_scripts.html:334-338` (palette entry)
- Test: `tests/test_web_studio.py`

**Interfaces:**
- Consumes: `domain.submit_directly` from Task 3 (available in the template context — the route passes the `StudioDomain` as `domain`).
- Produces: `window.STUDIO.submitDirect: bool`; non-admins on `/admin/studio/skill` see a `Publish` button that POSTs to the endpoint (not the suggestions queue).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_studio.py`:

```python
def test_skill_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body  # direct-submit domains publish, not suggest
    assert "Submit for approval" not in body
    assert "store" in body.lower()  # footer explains the store review pipeline


def test_skill_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the markdown textarea rendered


def test_existing_domains_keep_suggestion_flow(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/data-package", headers=_auth(seeded_app["analyst_token"]))
    assert "submitDirect: false" in resp.text
    assert "Submit for approval" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_studio.py -q`
Expected: the three new tests FAIL (`submitDirect` absent; footer button reads `Submit for approval` for non-admin skill).

- [ ] **Step 3: Implement the template + JS changes**

`app/web/templates/admin_studio.html` — footer block (~line 170), replace the note + button:

```html
    <div class="st-foot">
      <div class="st-result" id="studio-result"></div>
      <span class="note">{% if domain.submit_directly %}Published items go through the store's automated review before they appear.{% elif not is_admin %}An admin reviews your submission before it goes live.{% endif %}</span>
      <span class="spacer"></span>
      <button class="btn btn-primary" id="studio-create" type="button">{% if domain.submit_directly %}Publish{% elif is_admin %}Create{% else %}Submit for approval{% endif %}</button>
    </div>
```

Same file — `window.STUDIO` (~line 183), add the flag:

```html
  window.STUDIO = {
    profile: "{{ profile_slug }}",
    domain: "{{ domain.slug }}",
    endpoint: "{{ domain.endpoint }}",
    isAdmin: {{ 'true' if is_admin else 'false' }},
    submitDirect: {{ 'true' if domain.submit_directly else 'false' }},
    fields: [{% for f in domain.fields %}"{{ f.key }}"{% if not loop.last %}, {% endif %}{% endfor %}],
  };
```

`app/web/static/js/studio.js` — in `createEntity()` replace the role branch:

```js
  // Admins create directly; direct-submit domains (the store has its own
  // review pipeline) publish directly for everyone; otherwise non-admins
  // go to the moderation queue.
  if (!CFG.isAdmin && !CFG.submitDirect) return submitSuggestion(payload);
```

Also update the required-field guard at the top of `createEntity()` — skill has no `slug` field, so keep the existing `if (!payload.name && !payload.slug)` check as-is (name is required for skill; it still fires when both are empty).

`app/web/templates/_app_scripts.html` — add after the corporate-memory palette entry (line 337):

```js
          { label: 'Studio · Skill',              hint: 'authoring',    href: '/admin/studio/skill' },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_studio.py -q`
Expected: PASS (new + all pre-existing).

- [ ] **Step 5: Visual check (mandatory for web pages — see memory: always screenshot before calling UI done)**

Run the app locally (`uvicorn app.main:app --reload` with a seeded dev `DATA_DIR`, or the docker E2E stack), open `/admin/studio/skill`, and verify: nav chrome + CSS present, four fields render (two textareas, one select), footer reads `Publish`. The studio pages previously shipped unstyled because of a minimal template context (`_chrome_ctx` regression) — confirm the skill page inherits the fixed context (it does automatically via the shared `studio` route, but look at the page anyway).

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/admin_studio.html app/web/static/js/studio.js app/web/templates/_app_scripts.html tests/test_web_studio.py
git commit -m "feat(studio): skill builder page — direct publish flow + palette entry"
```

---

### Task 5: CLI — `agnes store publish-md`

**Files:**
- Modify: `cli/commands/store.py` (new command after `upload`, line ~68)
- Test: `tests/test_cli_store.py` (mirror the existing upload-command test pattern — read the file first and copy its mocking approach exactly)

**Interfaces:**
- Consumes: Task 1's endpoint; `api_post_json` + `V2ClientError` (already imported in `cli/commands/store.py:19-24`); `STORE_CATEGORIES` (already imported, line 17).
- Produces: `agnes store publish-md <name> <SKILL.md path> [--description …] [--category …]`.

- [ ] **Step 1: Write the failing test**

Open `tests/test_cli_store.py`, find how the existing `upload` test stubs the HTTP layer (CliRunner + monkeypatched `api_post_multipart` or a respx/httpx mock), and add the sibling with the same technique:

```python
def test_publish_md_posts_json(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli.main import app

    md = tmp_path / "SKILL.md"
    md.write_text("# My skill\n\nLong enough body for the CLI test.")
    captured = {}

    def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"id": "e1", "name": "my-skill", "version": 1, "visibility_status": "pending"}

    monkeypatch.setattr("cli.commands.store.api_post_json", fake_post)
    result = CliRunner().invoke(
        app,
        ["store", "publish-md", "my-skill", str(md), "--description", "Use when testing the CLI publish path"],
    )
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/store/entities/from-markdown"
    assert captured["payload"]["type"] == "skill"
    assert captured["payload"]["name"] == "my-skill"
    assert "My skill" in captured["payload"]["skill_md"]
    assert "Held for automated review" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_store.py::test_publish_md_posts_json -q`
Expected: FAIL — `No such command 'publish-md'` (exit_code 2).

- [ ] **Step 3: Implement the command**

In `cli/commands/store.py`, after `upload_entity`:

```python
@store_app.command("publish-md")
def publish_markdown(
    name: str = typer.Argument(..., help="Skill name (lowercase, digits, dashes)"),
    skill_md: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to the SKILL.md"
    ),
    description: Optional[str] = typer.Option(None, "--description"),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Category (case-insensitive). One of: " + ", ".join(STORE_CATEGORIES),
    ),
):
    """Publish a skill from a single Markdown file — no ZIP needed.

    The server wraps the file into the same guardrail + review pipeline as
    ``agnes store upload``.
    """
    payload: dict = {
        "type": "skill",
        "name": name,
        "skill_md": skill_md.read_text(encoding="utf-8"),
    }
    if description:
        payload["description"] = description
    if category:
        payload["category"] = category
    try:
        body = api_post_json("/api/store/entities/from-markdown", payload)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Published: id={body['id']} name={body['name']} version={body['version']}")
    if body.get("visibility_status") == "pending":
        typer.echo(
            f"Held for automated review — check progress with: agnes store status {body['id']} --wait"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/store.py tests/test_cli_store.py
git commit -m "feat(cli): agnes store publish-md — markdown-first skill publish"
```

---

### Task 6: MCP tool + triple-surface cohort entry

**Files:**
- Modify: `app/api/mcp_http.py` (new tool next to `store_rate`/`store_status`, ~line 377; add the name to the module's tool-name listing at line 17 if that comment/registry enumerates tools)
- Modify: `tests/test_documentation_api_triple_surface.py` (`_COHORT`, line ~29)
- Test: the ratchet test itself + whatever asserts the MCP tool set (`tests/test_mcp_http.py::test_exact_server_side_tool_set` — it enumerates tools, so it MUST be updated in the same commit)

**Interfaces:**
- Consumes: Task 1's endpoint; `_BASE`, `_headers()`, `httpx`, `@mcp.tool()` — all present in `app/api/mcp_http.py`.
- Produces: MCP tool `store_publish_markdown(name, skill_md, description=None, category=None)`.

- [ ] **Step 1: Extend the ratchet first (the failing test)**

In `tests/test_documentation_api_triple_surface.py`, add to `_COHORT`:

```python
    # Markdown-first skill publish (studio Skill Builder, issue #688).
    "/api/store/entities/from-markdown": ("store publish-md", "store_publish_markdown"),
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_documentation_api_triple_surface.py -q`
Expected: FAIL — MCP surface missing for the new endpoint (CLI half passes since Task 5 landed).

- [ ] **Step 3: Implement the MCP tool**

In `app/api/mcp_http.py`, after `store_status`:

```python
@mcp.tool()
async def store_publish_markdown(
    name: str,
    skill_md: str,
    description: str | None = None,
    category: str | None = None,
) -> dict:
    """Publish a skill to the store from Markdown content — no ZIP needed.

    The server synthesizes the SKILL.md folder and routes it through the same
    guardrail + review pipeline as a ZIP upload. The result may be held for
    automated review (``visibility_status: pending``) before it appears.

    Args:
        name:        Skill name — lowercase letters, digits, dashes.
        skill_md:    The SKILL.md content (frontmatter optional; synthesized
                     from ``name``/``description`` when absent).
        description: One-line *use when …* trigger (goes into frontmatter).
        category:    Optional store category (case-insensitive).

    Returns the created entity — ``{"id", "name", "invocation_name",
    "version", "visibility_status", …}``.
    """
    payload: dict = {"type": "skill", "name": name, "skill_md": skill_md}
    if description:
        payload["description"] = description
    if category:
        payload["category"] = category
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/store/entities/from-markdown",
            json=payload,
            headers=_headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()
```

Then update the expected-tool enumeration in `tests/test_mcp_http.py` (`test_exact_server_side_tool_set`) to include `store_publish_markdown`. (Heads-up from the release notes: this test is a known xdist ordering flake — verify it in isolation if the parallel run trips.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_documentation_api_triple_surface.py tests/test_mcp_http.py -q`
Expected: PASS (run `tests/test_mcp_http.py` solo if the xdist flake fires).

- [ ] **Step 5: Commit**

```bash
git add app/api/mcp_http.py tests/test_documentation_api_triple_surface.py tests/test_mcp_http.py
git commit -m "feat(mcp): store_publish_markdown tool + triple-surface cohort entry"
```

---

### Task 7: Browser E2E case, CHANGELOG, full-suite gate

**Files:**
- Modify: `tests/e2e/test_studio_web.py:38-53` (`CASES`)
- Modify: `CHANGELOG.md` (`## [Unreleased]` → Added)

**Interfaces:**
- Consumes: everything above; the E2E `CASES` tuple shape `(domain, {text-field: value}, {select-field: value})`.
- Produces: a green suite; a releasable PR.

- [ ] **Step 1: Add the E2E case**

In `tests/e2e/test_studio_web.py`, append to `CASES` (payload must clear the content floors — description ≥ 30 chars / ≥ 4 distinct words, body ≥ 200 chars):

```python
    (
        "skill",
        {
            "name": "e2e-skill",
            "description": "Use when exercising the studio skill builder end to end",
            "skill_md": (
                "Step one: open the page under test and confirm the layout. "
                "Step two: run the documented commands in order. "
                "Step three: verify the output matches the expected values and "
                "report any mismatch with the exact command and observed output."
            ),
        },
        {"category": "Other"},
    ),
```

Then check the success assertion: `test_builder_creates_entity` waits for `text=Created:` — for direct-submit domains `studio.js` prints the same `Created: <id>` line (the shared `createEntity` path), so no assertion change is needed. If Task 4 changed the success copy for publish domains, align the assertion here.

- [ ] **Step 2: Run the E2E locally (gated; skip-clean otherwise)**

Run: `AGNES_E2E=1 AGNES_E2E_DEV_MODE=1 .venv/bin/pytest "tests/e2e/test_studio_web.py::test_builder_creates_entity[skill]" -q`
Expected: PASS against the docker E2E stack (see `tests/e2e/docker-compose.e2e.yml`; use `AGNES_E2E_PORT` if 8000 is taken). Without the env gates the test skips — that is acceptable for CI parity, but run it for real once before the PR.

- [ ] **Step 3: CHANGELOG bullet**

Under `## [Unreleased]` → `### Added`:

```markdown
- Studio Skill Builder (`/admin/studio/skill`, issue #688): guided authoring of a SKILL.md with an assistant profile, published straight into the store's guardrail + review pipeline via the new `POST /api/store/entities/from-markdown` (also `agnes store publish-md` and the `store_publish_markdown` MCP tool). Direct-submit domains bypass the authoring-suggestions queue — the store's own review is the moderation.
```

- [ ] **Step 4: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: green, except failures that reproduce on a clean base branch (`git stash` to confirm; note them in the PR body per `docs/RELEASING.md`).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_studio_web.py CHANGELOG.md
git commit -m "feat(studio): skill builder E2E case + changelog"
```

---

## Follow-ups (explicitly NOT in this plan)

- **Improver flow** (the second half of #688): "improve an existing skill" — load an owned store entity's SKILL.md into the builder, edit with the assistant, publish as a new version via `PUT /api/store/entities/{id}`. Needs an entity picker and version-bump UX; file it as a follow-up issue referencing #688 (or extend #688) once the builder lands.
- **`references/` authoring**: the JSON contract deliberately carries one SKILL.md; multi-file skills keep using `agnes store upload` (ZIP). If demand shows up, extend `CreateFromMarkdownBody` with a `files: dict[str, str]` map — the synth-ZIP delegation absorbs it trivially.
- **Skill-curation linter** (#687) plugs naturally into the assistant profile later — the persona already encodes the trigger-quality rule.

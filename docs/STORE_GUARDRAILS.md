# Store / Flea-Market Upload Guardrails

Every `POST` / `PUT` to `/api/store/entities` (and the matching webapp
upload form) goes through a four-stage check pipeline before the entity
becomes visible in the public flea browse or the served Claude Code
marketplace. The goal: keep the open submission surface from leaking
secrets, shipping prompt-injection payloads, or dropping low-effort spam
into every analyst's `/plugin install` list.

This page is for two audiences:

- **Operators** wiring up an instance — what config knobs control the
  pipeline, how to read the admin triage page, and how cost scales with
  the model tier.
- **Uploaders** — what gets checked, what blocks publication, and how to
  iterate on a rejected upload.

---

## Architecture (one diagram)

```
POST /api/store/entities
  │
  ├── ZIP safety + size cap                          ─┐
  ├── (1) Manifest & metadata                          │ inline,
  ├── (2) Static security scan (regex + AST)           │ deterministic
  ├── (3) Quality + templating recommendation          │ (~50 ms)
  │                                                    │
  │   any inline check fails ─►  422 + structured detail
  │                              + store_submissions row (status='blocked_inline')
  │                              + audit_log entry
  │                              ▲ entity NOT created — bundle deleted from disk
  │
  ├── inline checks pass:
  │     create store_entities row (visibility_status='pending')
  │     create store_submissions row (status='pending_llm')
  │     BackgroundTasks.add_task(run_llm_review, …)
  │     return 202 with submission_id
  │
  └── BackgroundTasks worker (single-shot, ~5–30 s):
        (4) LLM security review (Anthropic, configurable tier)
          • on safe / low risk with no high|critical findings:
              status='approved'  + visibility flips to 'approved'
              → entity now appears in flea browse + served marketplace
          • on risky verdict:
              status='blocked_llm' + entity stays hidden
              → admin can override (audit-logged) or uploader can edit + resubmit
          • on LLM error / timeout:
              status='review_error' + retry_count++
              → admin can retry from /admin/store/submissions
```

The flea-market browse query (`GET /api/store/entities`) filters to
`visibility_status='approved'` for non-admin, non-self-owner callers.
Owners always see their own submissions regardless of state so they can
fix and resubmit; admins see everything. The same filter applies to
`UserStoreInstallsRepository.list_for_user`, so an installed entity that
gets blocked or hidden by review stops being served to Claude Code via
`marketplace.zip` / `marketplace.git` until an admin override.

---

## Configuration

`instance.yaml`:

```yaml
guardrails:
  # Master kill-switch. When false, inline checks still run (they're
  # free) but the LLM step is skipped and uploads auto-approve. Useful
  # for local dev without an LLM key.
  enabled: true

  # Anthropic model tier for the LLM security review.
  #   haiku  — ~$0.001/review, default, good enough for routine uploads
  #   sonnet — ~$0.015/review, deeper reasoning, fewer false negatives
  #   opus   — ~$0.075/review, only for high-stakes deployments
  # Or pin a concrete model ID like "claude-haiku-4-5-20251001".
  review_model: "haiku"

  # v30: per-submitter daily cap on inline-blocked uploads. Rejects
  # the upload at the API boundary (HTTP 429) when a single submitter
  # accumulates ≥ N blocked_inline rows in the trailing 24h. Bounds
  # disk + admin-queue spam from a bot looping on malformed ZIPs.
  # Set to 0 to disable.
  blocked_quota_per_day: 50

  # v30: how many days to keep blocked-bundle bytes on disk before
  # the daily TTL job purges them. Submission row + bundle_sha256 +
  # file_size always survive — only the bundle bytes get removed.
  # The detail UI then renders "Bundle purged on YYYY-MM-DD" instead
  # of the Download button. Set to 0 to retain forever (admin Delete
  # only).
  blocked_bundle_ttl_days: 30
```

Required environment variable (when guardrails enabled):

```
ANTHROPIC_API_KEY=sk-ant-…   # or LLM_API_KEY for the proxy case
```

### Three-state publish-gate matrix (fail-CLOSED)

The pipeline distinguishes **operator intent** (the YAML toggle) from
**provider readiness** (whether `ANTHROPIC_API_KEY` / `LLM_API_KEY` is
in the environment). The two are deliberately separate so a missing
env var can't silently flip an intended-on pipeline into auto-approve.

| `guardrails.enabled` | Provider key in env | Behavior |
|---|---|---|
| `false` | (any) | Pipeline OFF. Inline checks still run. Uploads auto-approve. Operator's explicit opt-out — local dev / no-LLM deployments. |
| `true` | yes | Normal hold-for-review. Inline + LLM both run. |
| `true` | **no** | **Hold-for-review, but no async worker fires.** Submissions land at `status='pending_llm'` and stay there until an admin either provides the key and clicks **Retry review** on `/admin/store/submissions/<id>`, or overrides + publishes the row manually. The entity stays at `visibility_status='pending'` (initial v1) or at the prior approved version (v2+ edits/restores). No silent auto-approval. A loud boot-time warning surfaces the misconfig in the logs. |

This is the **fail-CLOSED** policy. Before v45 the third row silently
auto-approved every upload as a "first-boot sanity" affordance — which
also meant a deployment whose operator forgot to set the key published
every upload without security review. The split was introduced after a
prod incident where an admin uploaded a skill containing a `curl … | sh`
exfiltration script and the system happily marked it `approved`.

---

## Accepted upload formats

Uploads accept either a `.zip` bundle or a single `.skill` file. A `.skill`
is just a lone `SKILL.md` document — YAML frontmatter with `name` and
`description` followed by a markdown body — and is materialized server-side
as `scratch/SKILL.md`, so it is exactly equivalent to a one-file skill ZIP.
Because a single file cannot carry `scripts/` / `references/` / `assets/`
subdirs, `.skill` uploads are only valid for `type=skill`; uploading one as
`type=plugin` or `type=agent` is rejected with HTTP 422
(`skill_file_wrong_type`). The upload path is detected purely by filename
suffix; everything downstream (validation, bake, guardrails) is
format-agnostic.

## What gets checked

### 1. Manifest & metadata (inline, deterministic)

| Rule | Skill | Plugin | Agent |
|------|:-----:|:------:|:-----:|
| Required file present (`SKILL.md` / `.claude-plugin/plugin.json` / `*.md`) | ✓ | ✓ | ✓ |
| `plugin.json` parses as valid JSON | — | ✓ | — |
| `plugin.json.name` matches `[a-zA-Z0-9_-]{1,64}` | — | ✓ | — |
| `plugin.json.version` (if present) matches loose semver | — | ✓ | — |
| Bundle within `MAX_ZIP_SIZE` (50 MB) and `MAX_ZIP_UNCOMPRESSED` (200 MB) | ✓ | ✓ | ✓ |

Failure here is a **hard block**: HTTP 422 with the failing rule names.

### 2. Static security scan (inline, deterministic)

> **Static scan is signal, not gate.** Regex matches flag candidates
> for the LLM reviewer; treat them as suggestive, not authoritative. Any
> attacker willing to obfuscate (`getattr(__builtins__, "ev"+"al")`,
> dynamic imports, base64-decoded eval) trivially bypasses substring
> matching. The pipeline still inline-blocks on a finding because
> shipping known-bad patterns to the LLM is wasteful — but operators
> reading `inline_checks.static_security` should NOT assume "no
> findings" means "safe". The LLM verdict carries that determination.

Regex patterns aimed at high-confidence danger signals. False positives
exist; admin override is the recovery path. Documentation files (`.md`,
`.txt`, `.rst`, `.html`, `.json`, `.yaml`, `.yml`, `.toml`) are skipped
to avoid flagging prose that legitimately discusses `eval`/`exec`. Code
files (`.py`, `.js`, `.sh`, …) remain in scope.

- **Code execution** — `eval(`, `exec(`, `os.system(`, bash `eval $X`,
  `subprocess.run(... shell=True ...)`, `pickle.loads(`, base64-decoded
  payloads passed to eval/exec.
- **Hardcoded secrets** — Anthropic / OpenAI keys (`sk-…`), AWS access
  keys (`AKIA…`), GitHub PATs (`ghp_…`), Slack tokens (`xox[bapres]-…`),
  embedded RSA / EC / OpenSSH private-key blocks.
- **Destructive filesystem ops** — `rm -rf $HOME` / `~` / `/`,
  `shutil.rmtree($HOME)`.
- **Path traversal** — sequences of three or more `../` segments.
- **Reverse shells / suspicious networking** — bash reverse-shell idiom
  (`bash -i >& /dev/tcp/…`), netcat with listen flags, raw IP URLs,
  `.onion` URLs in scripts.

**Template-aware.** Lines whose only "exec-like" tokens come from inside
a `{{...}}` Jinja-style placeholder are stripped before pattern matching
— first-use customization is a feature, not an exfil vector.

Any finding here blocks publication. The 422 response cites every match
with file + line + reason so the uploader can fix and resubmit.

### 3. LLM security review (async, configurable tier — Haiku default)

A single-shot agentic review over the baked plugin tree. Reads the
manifest, primary doc, and every text file in the bundle (capped at
50 KB total prompt payload, with the most signal-dense files
prioritised). The model returns strict JSON:

```json
{
  "risk_level": "safe|low|medium|high|critical",
  "summary": "...",
  "findings": [
    {"severity": "high", "category": "exfiltration",
     "file": "run.sh", "explanation": "...", "fix_hint": "..."}
  ],
  "template_placeholders_found": 3
}
```

Pass condition: `risk_level IN (safe, low)` **and** no individual finding
has severity `high|critical`. Medium findings under a safe verdict pass
through (the "noise but no exploit" band you opt into when picking
Haiku). Operators who want a stricter floor pin Sonnet or Opus.

Cost: scales with the chosen tier. At Haiku rates a typical 20 KB plugin
costs ~$0.001 per review. At Opus rates ~$0.075. Re-uploads (PUT with a
new bundle) re-run the review; description-only edits do not.

The system prompt explicitly tells the model to ignore Jinja-style
`{{var}}` placeholders as benign and not to invent findings to look
thorough. The full prompt lives in `src/store_guardrails/prompts.py`.

### 4. Quality + templating (inline, deterministic, never blocks)

- Description ≥ 20 chars.
- Primary doc (`SKILL.md` / `agent.md`) ≥ 200 chars.
- AI-slop heuristics — flags `lorem ipsum`, `<INSERT_X_HERE>`, lone
  `TODO:` lines.
- **Templating recommendation** — counts `{{var}}` tokens across `.md`,
  `.json`, `.yaml`, `.sh`, `.py`, `.txt` files. If zero, the response
  carries a non-blocking hint: *"Consider adding `{{...}}` placeholders
  for user-specific values (project IDs, channel names, key contacts).
  Agnes will prompt the user to fill them in on first install — your
  skill becomes much more effective with parameterization."*

Quality is a `warn` status — these issues surface in the 422 response or
admin UI but never block on their own.

---

## Verdict vs. lifecycle — two axes

The pipeline writes to two columns and they mean different things:

| Axis        | Column                              | Values                                                                                                | Mutability |
|-------------|-------------------------------------|-------------------------------------------------------------------------------------------------------|------------|
| **Verdict** | `store_submissions.status`          | `pending_inline`, `blocked_inline`, `pending_llm`, `approved`, `blocked_llm`, `review_error`, `overridden`, `deleted` | Immutable forensic record of what was decided at review time. Set once, never re-flipped to track later lifecycle changes. |
| **Lifecycle** | `store_entities.visibility_status` | `pending`, `approved`, `hidden`, `archived`                                                           | Live state. Flipped by Archive (owner soft-delete), admin override (un-archive), rescan, future bulk ops. |

`'deleted'` is the one verdict value that does double duty —
hard-delete drops the entity row, so the JOIN can't reach it; the
submission keeps an explicit `'deleted'` marker so the *Deleted*
chip can surface the row.

The admin queue at `/admin/store/submissions` filters lifecycle via
LEFT JOIN on `store_entities` rather than reading a denormalized
column. The *Archived* chip translates to
`entity.visibility_status = 'archived'`; the default queue excludes
that and `status='deleted'`. Any code path that flips entity
visibility (admin override, manual SQL fix, future workflows) shows
up in the queue immediately — no backfill required, no drift surface.

The submission detail page renders **Status (verdict)** and
**Entity lifecycle** side by side, so admins triaging a row see
"this was approved at review time, but it's now archived" at a
glance.

---

## Admin triage — `/admin/store/submissions`

Every submission row is visible here, newest first, filterable by
status. For each row admins see who, what type, name + version,
status badge, the inline-check verdicts, the LLM findings (when the
review has run), and which model produced the verdict.

Action buttons:

- **Override** (on `blocked_inline`/`blocked_llm`/`review_error`) —
  Force-publishes the entity. Requires a reason ≥ 4 chars; reason +
  prior verdict are both written to `audit_log` so the trail of "who
  force-published what, and why" is permanent.
- **Rescan** (any submission with a live bundle) — Re-runs all checks
  (inline + LLM) against the current bundle. Use after check rules
  change to re-evaluate prior verdicts.
- **Retry LLM** (on `review_error` / `blocked_llm`) — Re-queues the
  LLM review only. Useful when the model timed out or the verdict
  looks like a false negative under a different model tier (bump
  `guardrails.review_model` and retry).
- **Download bundle** (any submission with a live, un-purged bundle) —
  Streams the on-disk bundle as a fresh ZIP for forensic inspection.
  Audit-logged.
- **Delete** — Hard-deletes the submission row + the bundle on disk +
  any installs. Audit row preserves what was deleted.

### Retention model (v30)

Blocked bundles persist on disk so admins can Rescan / Override /
Download for as long as they're useful. The daily TTL job
(`store-blocked-purge`, runs at 04:00 UTC against
`POST /api/admin/run-blocked-purge`) removes the bundle bytes once
the submission's `created_at` is older than
`guardrails.blocked_bundle_ttl_days` (default 30) AND the status is
still in `{blocked_inline, blocked_llm, review_error}`. Approved and
overridden submissions are never purged.

What survives the purge:
- The submission row (audit trail)
- `bundle_sha256` — for cross-submission correlation
- `file_size` — so the size column stays sortable

What goes away:
- The bundle directory under `${DATA_DIR}/store/<entity_id>/`
- The `store_entities` row (it's hidden; nothing references it)
- `entity_id` is nulled on the submission row

The detail page renders *"Bundle purged on YYYY-MM-DD"* in place of
the Download button so admins know why action is unavailable.

For privacy-sensitive accidental uploads (a submitter pasted a
secret), admins can use **Delete** on the detail page to remove the
bundle (and the row) immediately rather than waiting for the TTL.

To bound spam, `guardrails.blocked_quota_per_day` (default 50)
returns HTTP 429 `quota_exceeded` when a single submitter has ≥ N
inline-blocked rows in the last 24h. Set to 0 to disable.

The `/admin/scheduler-runs` page already shows scheduler-driven audit
events; submission events live alongside them in `audit_log` under the
actions:

```
store.submission.accepted
store.submission.blocked_inline
store.submission.approved
store.submission.blocked_llm
store.submission.review_error
store.submission.overridden
store.submission.bundle_downloaded
store.submission.rescan
store.submission.retry
store.submission.deleted
run_blocked_purge
store.submission.retry
store.submission.deleted
```

---

## Uploader-facing 422 contract

A blocked submission returns a structured detail the upload UI can
render directly:

```json
{
  "detail": {
    "code": "submission_blocked",
    "submission_id": "abcd…",
    "checks": {
      "manifest":        {"status": "pass"},
      "static_security": {"status": "fail",
                          "findings": [{"file": "run.sh", "line": 12,
                                        "category": "code_exec",
                                        "severity": "high",
                                        "reason": "shell eval expanding a variable",
                                        "snippet": "eval $1"}]},
      "quality":         {"status": "warn",
                          "template_placeholders": 0,
                          "template_recommendation": "Consider adding {{...}} placeholders ..."}
    }
  }
}
```

The submission row stays in the admin queue under
`status='blocked_inline'` so admin triage can see what people tried to
upload (useful for telemetry on what to harden checks against).

---

## Pre-submit dry-run

`POST /api/store/entities/dryrun` runs the **full pipeline** (inline
checks + LLM review) against a candidate bundle and returns the findings
**without persisting anything** — no `store_entities` row, no
`store_submissions` row, no `audit_log` entry, no bundle left on disk.

It exists so a submitter can iterate on a draft before the real upload:
see what would block publication, fix it, and only `POST
/api/store/entities` once the bundle is clean. Without it, every
iteration burns LLM tokens, counts against the blocked-upload quota, and
files an admin queue entry the admin then has to triage.

- **Payload** — same multipart form as the real upload: `file` (the ZIP),
  `type` (`skill` | `agent` | `plugin`), `description` (optional).
- **Auth** — `Depends(get_current_user)`, the same gate as
  `POST /api/store/entities`. Never anonymous: an open dry-run would be a
  free LLM proxy.
- **Response**

  ```json
  {
    "inline_checks": { "manifest": …, "static_security": …, "content": …, "quality": … },
    "llm_findings":  { "risk_level": …, "summary": …, "findings": […], … },
    "would_publish": true
  }
  ```

  `would_publish` is the AND of the inline verdict (`InlineResult.passed`)
  and the LLM `is_safe(verdict)` decision — exactly the condition the real
  create path uses to flip an entity to `approved`. `inline_checks` is the
  same shape returned in the [422 contract](#uploader-facing-422-contract);
  `llm_findings` is the raw verdict dict (`null` when guardrails are
  disabled or the LLM provider has no credentials — in which case
  `would_publish` rests on the inline tier alone).

The bundle bytes are extracted to a scratch dir, baked into a throwaway
plugin tree, checked, and wiped in `finally` — identical lifecycle to the
`/entities/preview` wizard step.

**Deferred (tracked on #317):** a per-submitter dry-run quota (so the
endpoint can't be looped to burn unlimited LLM tokens) and
identical-bundle verdict caching (`bundle_sha256 + review_model` →
reuse the previous verdict). Until those land, the auth gate plus the
HTTP-level rate limiter bound abuse. The endpoint is REST-only: it's a
web-form helper with no analyst CLI/MCP analogue (the real create
endpoint carries the triple-surface contract).

---

## Disabling the pipeline

Three ways:

1. `guardrails.enabled: false` in `instance.yaml` — explicit operator
   choice. Inline checks still run; LLM step + pending hold are skipped.
2. Don't set `ANTHROPIC_API_KEY` / `LLM_API_KEY` — auto-falls back to
   disabled with a startup warning.
3. Per-submission admin override — for known-good uploads that trip a
   false positive.

There is no per-uploader bypass and no bypass for admins on their own
uploads. Admins do see their own pending submissions in the flea browse
(filter shortcut), but the visibility flip still requires either review
approval or override.

---

## Extending the check set

Adding a new inline rule:

1. Add the rule to `src/store_guardrails/static_scan.py:_RULES` (or a
   new `*_check.py` for a different category).
2. Add a test fixture to
   `tests/test_store_guardrails_inline.py:TestStaticScan` covering the
   true-positive case.
3. Confirm the template-aware path doesn't strip your rule's trigger
   tokens — the `_TEMPLATE_RE` substitution happens BEFORE pattern
   matching, so a rule that fires on text that's only ever inside
   `{{...}}` will never trip.

Tightening the LLM verdict floor:

- Bump `guardrails.review_model` to `sonnet` or `opus` — same prompt,
  more reasoning budget.
- Or change the pass condition in
  `src/store_guardrails/llm_review.py:is_safe` (e.g. reject `medium`
  findings outright). Update tests in
  `tests/test_store_guardrails_llm.py` to match.

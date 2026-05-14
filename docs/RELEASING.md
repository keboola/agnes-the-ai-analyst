# Releasing & deploying

The full release process for Agnes. CLAUDE.md carries the short version; this
doc is the operational reference. Read it linearly the first few times — once
internalized, the order matters less, but the **non-obvious gotchas never go
away**.

## Changelog discipline — non-negotiable

**Every PR that adds, removes, or changes user-visible behavior MUST update
`CHANGELOG.md` in the same PR.** No exceptions, no follow-ups, no "I'll do it
after merge". User-visible = anything an operator, end-user, or downstream
integrator can observe: CLI flags / output / exit codes, REST endpoints /
payloads / status codes, web UI, `instance.yaml` schema, env vars,
`extract.duckdb` contract, Docker / compose / Caddyfile knobs, default
behaviors, breaking changes, security fixes.

**How:**
- Add a bullet under the topmost `## [Unreleased]` heading (create one if
  missing — it sits above the latest released version).
- Group by `### Added` / `### Changed` / `### Fixed` / `### Removed` /
  `### Internal` (Keep-a-Changelog sections).
- Mark breaking changes with `**BREAKING**` at the start of the bullet —
  operators grep for that string before bumping the pin.
- Reference the relevant doc/runbook if one exists (e.g.
  `see docs/auth-groups.md`), don't restate it.
- Internal-only changes (refactors, test additions, dependency bumps without
  behavior change) go under `### Internal` — still log them, just keep them
  terse.

Reviewers should bounce PRs that touch user-visible behavior without a
changelog update — same way they'd bounce a PR with no test changes for new
logic.

## Release-cut belongs to the PR — non-negotiable

**The version bump + CHANGELOG rename + new empty `[Unreleased]` are the LAST
commit on the PR that earned the version. Never a standalone follow-up PR.**

When a PR lands the only `[Unreleased]` content (or is the last in a queue of
in-flight feature PRs), the release-cut MUST ship as part of the same merge.
Standalone release-cut PRs add review-overhead PRs to history with no behavior
change of their own and pollute `git log` with bookkeeping commits separated
from the work that earned them.

**Mandatory checklist before approving / enabling auto-merge on ANY PR:**

1. **Stop.** Will this PR land alone in `[Unreleased]` (no other in-flight PRs
   queued behind it)?
2. **If yes**, the release-cut is REQUIRED in the same PR before merge. BEFORE
   pushing the final commit:
   - Bump `pyproject.toml` to `X.Y.Z`
   - Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`, add a new empty
     `## [Unreleased]` on top
   - Either squash these into the consolidation commit OR add as a separate
     `release: X.Y.Z` commit on the same branch
3. **THEN** push, approve, enable auto-merge.
4. After auto-merge fires: tag `vX.Y.Z` against the merge commit + create a
   GitHub Release. Done — one PR, one merge, one release.

**Failure mode to avoid:** enabling auto-merge on the feature PR thinking "I'll
add the release-cut after." Auto-merge fires faster than the second commit
lands. The window closes; the only fix is a standalone release-cut PR — exactly
what this rule prohibits.

**Acceptable standalone release-cut** (rare): only when `[Unreleased]`
accumulated bullets from MULTIPLE already-merged PRs AND no further
behavior-change PR is queued — i.e. the cut is the only outstanding work and
there's no PR to attach it to.

## Release workflow — concrete recipe

### Happy path (8 steps)

```bash
# 1. Branch from a fresh checkout. iCloud Drive worktrees randomly hang
#    on git operations — use a fresh shallow clone in /tmp instead.
cd /tmp && git clone --depth 50 --branch main \
  https://github.com/keboola/agnes-the-ai-analyst.git agnes-<topic>
cd agnes-<topic> && git checkout -b zs/<branch-name>

# 2. Make the change + tests. Run the AREA pytest while iterating
#    (e.g. `pytest tests/test_X.py -p no:xdist -q`).

# 3. Add a CHANGELOG bullet under [Unreleased].
#    Group: Added | Changed | Fixed | Removed | Internal
#    Mark BREAKING with **BREAKING** prefix.

# 4. Commit the change(s). Multiple logical commits OK; release-cut
#    will be a SEPARATE last commit (next step). DO NOT bundle the
#    release-cut into the same commit as the change — it pollutes
#    the SHA that auto-close keywords reference and makes revert
#    targeted at the change-only difficult.

# 5. Run the full pytest suite locally:
#    `pytest tests/ -p no:xdist -q` (or `-n auto` if xdist works).
#    Pre-existing fails (e.g. test_readers_in_pre_init_dir under
#    subprocess timeout) are OK to ignore; verify by reverting your
#    diff and reproducing on bare main.

# 6. Release-cut commit (LAST commit on the PR per the rule above):
#    - Bump pyproject.toml: version = "X.Y.Z"
#    - Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`
#    - Add a fresh empty `## [Unreleased]` line above
#    Commit message: `release: X.Y.Z — <one-line summary>`

# 7. Push branch + open PR + enable auto-merge SQUASH:
#    git push -u origin HEAD
#    gh pr create --repo keboola/agnes-the-ai-analyst \
#      --head <branch> --title "<...>" --body "<...>"
#    gh pr merge <N> --repo keboola/agnes-the-ai-analyst \
#      --squash --auto --delete-branch

# 8. After auto-merge fires (poll or `Monitor`):
#    git fetch origin --tags
#    git tag vX.Y.Z <merge-sha>
#    git push origin vX.Y.Z
#    gh release create vX.Y.Z --repo keboola/agnes-the-ai-analyst \
#      --title "vX.Y.Z — <...>" --notes "<copy-paste from CHANGELOG>"
```

### Picking the next version

`pyproject.toml`'s current `version` is the **next-release target** (post-cut
from the previous release). Pre-1.0 we patch-bump for everything that doesn't
break operator-facing APIs:

- `instance.yaml` schema additions, new env vars, new endpoints → patch (e.g.
  0.54.3 → 0.54.4)
- New CLI subcommands, BREAKING removals, schema migrations → still patch within
  the current 0.5x cycle (no minor bumps cut today)
- The CHANGELOG `**BREAKING**` marker is what operators grep for; the version
  number is secondary

Always check `git tag -l "v0.X*"` before naming — if `v0.54.0` is already
tagged, the next one is `v0.54.1`, even if `pyproject.toml` still says `0.54.0`
from a stale post-cut commit (we've shipped that race before).

### Authoring expectations on the PR

- **Self-PRs** (you're both author and reviewer): GitHub forbids self-approve.
  If branch protection requires N approving reviews (we don't today —
  `required_approving_review_count = 0`), you need someone else to approve. With
  our current 0-review setup, self-PRs can still merge automatically once
  required CI passes.
- **Other people's PRs you're taking over**: dismiss any prior
  CHANGES_REQUESTED reviews (yours or someone else's) before auto-merge can
  fire. `gh pr review <N> --approve --body "..."` after pushing your fixes.
- **Devin Review**: not a required check today; runs in parallel and posts a
  comment. Don't wait on it for merge unless the human reviewer explicitly asks.

### CI quirks you WILL hit

- **`gh pr checks` glosses CANCELLED as `fail`.** When you force-push (rebase,
  amend), GitHub auto-cancels the in-flight `Release` workflow run on the older
  SHA. Those cancelled jobs show up as "fail" in the PR's check summary and tab
  forever, even after newer runs succeed. **Look at the conclusion column, not
  just the count.** Rule of thumb: if the same check name appears with both
  `pass` and `fail` rows, the `fail` row is from an older auto-cancelled SHA.
  Verify with `gh api repos/keboola/agnes-the-ai-analyst/commits/<sha>/check-runs`
  — the raw API distinguishes `cancelled` from `failure` truthfully.
- **Branch protection's "strict" mode caches cancelled `test` as blocking** even
  after newer `test` runs succeed. Symptom: `mergeable_state: blocked` despite
  all required checks green on the latest SHA. Fix: re-run the cancelled
  `Release` workflow run (`gh run rerun <run-id>`); once its `test` job lands as
  success, the block clears. We've hit this on PRs #273, #281, #285, #286.
- **Required checks** (per branch protection): `test` + `docker-build` only.
  Other workflows (`cli-wheel-clean-install`, `build-and-push`,
  `Release`-pipeline, Devin Review) are advisory — green/red doesn't gate merge.
- **`enforce_admins: true`** in branch protection means `--admin` flag on
  `gh pr merge` does NOT bypass. Don't try; just fix the underlying block.

### Recovery when something derails

- **Force-pushed and lost auto-merge?** GitHub *usually* preserves auto-merge
  across force-pushes for the same PR; if it cleared, just re-run
  `gh pr merge <N> --squash --auto --delete-branch`.
- **Release-cut commit forgot to land?** That's the failure mode the
  "Release-cut belongs to the PR" rule prevents. If it happens anyway: open a
  follow-on PR with ONLY the release-cut commit, ship it, and write up why in
  your post-mortem comment.
- **Wrong version number tagged?** `git tag -d vX.Y.Z && git push --delete
  origin vX.Y.Z` then re-tag against the right SHA. Update the GitHub Release if
  you already created it.

## Deploy workflows

Two separate release.yml-style workflows produce GHCR images. Pick the one that
matches what you're shipping.

### `release.yml` — auto-build on every push

Runs on **every** push to **every** branch.
- Push to `main` → `:stable`, `:stable-YYYY.MM.N` (CalVer).
- Push to non-main `<prefix>/<branch>` → `:dev`, `:dev-YYYY.MM.N`,
  `:dev-<branch-slug>`, and (when prefix isn't a Git Flow convention)
  `:dev-<prefix>-latest` alias.

VMs that pin to a floating tag (`:dev`, `:dev-<prefix>-latest`) auto-upgrade
within ~5 min via the cron in `agnes-auto-upgrade.sh`. Convenient for
per-developer dev VMs; **footgun for shared dev VMs** (last pusher wins,
regardless of who).

### `keboola-deploy.yml` — tag-triggered, explicit deploy only

Runs **only** on git tags matching `keboola-deploy-*`. Publishes:
- `:keboola-deploy-<git-tag-suffix>` — immutable, tied to the exact commit
- `:keboola-deploy-latest` — floating alias the consumer pins to

**Operator workflow:**
```bash
git checkout <commit-or-branch>
git tag keboola-deploy-<descriptive-name>
git push origin keboola-deploy-<descriptive-name>
# → workflow builds + publishes both tags
# → VM cron picks up :keboola-deploy-latest within ~5 min
# → manual cron trigger (skip the wait): sudo /usr/local/bin/agnes-auto-upgrade.sh on the VM
```

Use this when the consumer (e.g. a customer dev VM) needs
**deploy-when-I-decide** semantics — no surprise rollouts from upstream branch
pushes by other contributors. The infra repo pins
`image_tag = "keboola-deploy-latest"` on the relevant VM.

### Module versioning

The customer-instance Terraform module under `infra/modules/customer-instance/`
is published as `infra-vMAJOR.MINOR.PATCH` git tags (separate from app CalVer
tags). Bump on any module-API change; downstream infra repos pin to the tag in
their `source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.X.Y"`.

After merging a module change to `main`:
```bash
git tag infra-vX.Y.Z origin/main
git push origin infra-vX.Y.Z
```

### Replacing a VM after a startup-script change

Module sets `lifecycle { ignore_changes = [metadata_startup_script] }` on
`google_compute_instance.vm` so normal `terraform apply` doesn't churn running
VMs. To propagate a startup-script update, trigger the consumer's apply workflow
manually with the VM resource address — typical workflow_dispatch input is
`recreate_targets='module.agnes.google_compute_instance.vm["<vm-name>"]'`.

## Appendix: CHANGELOG entry skeleton

Copy this when adding to `## [Unreleased]` in `CHANGELOG.md`. Drop the sections
you don't need; keep the Keep-a-Changelog order.

```markdown
### Added
- New feature description.

### Changed
- Change description. **BREAKING** prefix + migration steps if operator-facing.

### Fixed
- Bug fix description.

### Removed
- **BREAKING** removed feature — what replaces it.

### Internal
- Refactors, test additions, dependency bumps with no behavior change.
```

At release-cut time `## [Unreleased]` is renamed to `## [X.Y.Z] — YYYY-MM-DD`
and a fresh empty `## [Unreleased]` is added on top. CI publishes the matching
`stable-YYYY.MM.N` image tag for the merge commit (see Deploy workflows above).

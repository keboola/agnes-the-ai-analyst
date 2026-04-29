# Release & CI/CD Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the release/image/changelog stack from three drifting sources of truth and four overlapping workflows down to a coherent pipeline: git tags drive package versions, GitHub Releases hold release notes, one reusable workflow runs the test suite, image-tag sprawl is bounded, and operator-facing image policies are explicit instead of emergent.

**Architecture:** Replace the manually-maintained `CHANGELOG.md` + hardcoded `pyproject.toml` version + 475-tag-per-month CalVer race-loop with three single-source-of-truth mechanisms: (1) `setuptools_scm` reads version from `git describe --match 'v*'`; (2) Release Drafter aggregates merged-PR titles into a draft GitHub Release; (3) image tags use `${{ github.run_number }}` and a deliberate per-branch policy. A single reusable workflow (`_test.yml`) runs tests; release.yml, keboola-deploy.yml, and ci.yml all `uses:` it. Auto-rollback moves to its own callable workflow so it's testable and triggerable manually.

**Tech Stack:** GitHub Actions (reusable workflows + workflow_call + workflow_dispatch), `setuptools_scm`, Release Drafter (`release-drafter/release-drafter` action), Docker buildx, GHCR, `actionlint` for YAML validation.

**Operator-impact note:** Phase 5 changes how dev VMs receive images. Anyone with a VM pinned to `:dev-<prefix>-latest` will need to either (a) re-pin to `:dev` (latest dev across all branches), or (b) use `workflow_dispatch` to publish a per-branch image when they want to deploy non-main code. Migration is documented in Task 11.

---

## Phase Map

| Phase | Tasks | Risk | Operator-visible? |
|-------|-------|------|-------------------|
| 1. Cleanup | 1–2 | none | no |
| 2. Reusable test workflow | 3–4 | low | no |
| 3. Versioning from git | 5–7 | medium | yes (`/api/version`, `da --version`) |
| 4. Release notes automation | 8 | low | yes (Releases page) |
| 5. Image pipeline simplification | 9–12 | **high** | **yes (dev VMs, tag scheme)** |
| 6. Documentation pass | 13 | none | no |

Stop after each phase for review before proceeding.

---

## File Structure

**Files created:**
- `.github/workflows/_test.yml` — reusable test workflow (`on: workflow_call`)
- `.github/workflows/release-drafter.yml` — Release Drafter trigger
- `.github/release-drafter.yml` — Release Drafter config (categories, version resolver)
- `.github/workflows/rollback.yml` — manual + workflow_call rollback of `:stable`
- `scripts/ops/prune-dev-tags.sh` — periodic dev-tag pruner
- `.github/workflows/prune-dev-tags.yml` — scheduled wrapper for prune script
- `docs/release-process.md` — operator-facing doc replacing the deleted `RELEASE_TEMPLATE.md`

**Files modified:**
- `pyproject.toml` — `dynamic = ["version"]` + `[tool.setuptools_scm]` config
- `.github/workflows/release.yml` — uses `_test.yml`, drops CalVer race + per-branch logic + inline rollback, version from build context
- `.github/workflows/keboola-deploy.yml` — uses `_test.yml`, version from setuptools_scm
- `.github/workflows/ci.yml` — uses `_test.yml`
- `Dockerfile` — accepts AGNES_VERSION build arg from setuptools_scm-derived value
- `CLAUDE.md` — "Release & deploy workflows" section rewritten

**Files deleted:**
- `CHANGELOG.md` (already deleted in worktree, awaiting commit)
- `docs/RELEASE_TEMPLATE.md` (already deleted in worktree, awaiting commit)
- `.github/workflows/deploy.yml` (dead "SUPERSEDED" stub)

---

# Phase 1 — Cleanup

## Task 1: Commit existing CHANGELOG removal

**Context:** The worktree already has staged-but-uncommitted changes from the prior conversation step: `CHANGELOG.md` deleted, `docs/RELEASE_TEMPLATE.md` deleted, `CLAUDE.md` "Changelog discipline" section rewritten as "Release notes" section pointing at GitHub Releases.

**Files:**
- Delete: `CHANGELOG.md`
- Delete: `docs/RELEASE_TEMPLATE.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Verify worktree state**

```bash
cd ../tmp_oss-release-cleanup
git status --short
# Expected: D CHANGELOG.md, M CLAUDE.md, D docs/RELEASE_TEMPLATE.md
git diff --stat HEAD
# Expected: ~3 files, ~789 deletions, ~5 insertions
```

- [ ] **Step 2: Verify no dangling code references to CHANGELOG.md as a file**

Run: `grep -rn "CHANGELOG\.md" --include='*.py' --include='*.yml' --include='*.yaml' --include='*.toml' --include='*.sh' . | grep -v 'docs/superpowers/'`

Expected: zero hits in production code (Jira-connector hits for `CHANGELOG_SCHEMA` are unrelated — that's a Jira-issue-history schema, not project release notes).

If any production code references `CHANGELOG.md` as a file path, list and discuss before proceeding.

- [ ] **Step 3: Commit**

```bash
git add -A CHANGELOG.md docs/RELEASE_TEMPLATE.md CLAUDE.md
git commit -m "chore: drop CHANGELOG.md, move release notes to GitHub Releases

The hand-maintained CHANGELOG.md drifted (3 versions duplicated, one out of order, version disagreed with pyproject.toml and git tags). Release notes belong in GitHub Releases — a single source the rest of the pipeline can drive automatically. CLAUDE.md 'Changelog discipline' section is replaced with 'Release notes' guidance pointing at the Releases page and the per-PR title contract.

History remains in git; the deleted file is recoverable via git log."
```

---

## Task 2: Delete `deploy.yml` (dead "SUPERSEDED" stub)

**Files:**
- Delete: `.github/workflows/deploy.yml`

- [ ] **Step 1: Confirm nothing references deploy.yml**

Run: `grep -rn "deploy\.yml\|workflow.*deploy" .github/ docs/ scripts/ CLAUDE.md README.md`
Expected: only the `keboola-deploy.yml` and `propagate-infra-tag.yml` filenames; no references to the workflow named `deploy.yml` itself.

- [ ] **Step 2: Confirm there are no recent runs**

```bash
gh run list --workflow=deploy.yml --limit 5 || true
```
Expected: empty list or only ancient runs. If recent runs exist, verify with the user that they were intentional.

- [ ] **Step 3: Delete**

```bash
git rm .github/workflows/deploy.yml
```

- [ ] **Step 4: Commit**

```bash
git add -A .github/workflows/deploy.yml
git commit -m "ci: delete superseded deploy.yml stub

The file was marked 'SUPERSEDED by release.yml' in its first comment line and only kept a manual workflow_dispatch handler that ran tests with no publish step. Anyone needing a manual test run uses ci.yml or release.yml's workflow_dispatch."
```

---

# Phase 2 — Reusable test workflow

## Task 3: Create `_test.yml` reusable workflow

**Files:**
- Create: `.github/workflows/_test.yml`

- [ ] **Step 1: Author the reusable workflow**

```yaml
# .github/workflows/_test.yml
name: _Test (reusable)

# Single source of truth for the test job. Called from ci.yml,
# release.yml, and keboola-deploy.yml so the install/lint/typecheck/pytest
# recipe lives in exactly one place.

on:
  workflow_call:
    inputs:
      python-version:
        type: string
        default: "3.13"
      run-lint:
        type: boolean
        default: true
      run-typecheck:
        type: boolean
        default: true
      pytest-args:
        type: string
        default: "-v --tb=short -n auto"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0          # setuptools_scm needs full history
          fetch-tags: true

      - uses: actions/setup-python@v6
        with:
          python-version: ${{ inputs.python-version }}

      - name: Install uv
        uses: astral-sh/setup-uv@v7

      - name: Install dependencies
        run: uv pip install --system ".[dev]"

      - name: Lint with ruff
        if: inputs.run-lint
        run: |
          pip install ruff
          ruff check . || true
        continue-on-error: true

      - name: Type check with mypy
        if: inputs.run-typecheck
        run: |
          pip install mypy
          mypy src/ app/ cli/ connectors/ --ignore-missing-imports --no-error-summary || true
        continue-on-error: true

      - name: Run tests
        run: pytest tests/ ${{ inputs.pytest-args }}
        env:
          TESTING: "1"
```

- [ ] **Step 2: Validate YAML with actionlint**

Run:
```bash
docker run --rm -v "$(pwd):/repo" rhysd/actionlint:latest -color /repo/.github/workflows/_test.yml
```
Expected: no errors. (If `actionlint` isn't available locally, push the branch and let GitHub validate at parse time — a syntax error fails the calling workflow's startup.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/_test.yml
git commit -m "ci: add reusable _test.yml workflow

Single source of truth for the install + lint + mypy + pytest recipe. ci.yml, release.yml, and keboola-deploy.yml each carried near-identical copies — three places to drift. Inputs allow callers that don't want lint/typecheck (pure release jobs) to skip them."
```

---

## Task 4: Wire `ci.yml`, `release.yml`, `keboola-deploy.yml` to use `_test.yml`

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `.github/workflows/keboola-deploy.yml`

- [ ] **Step 1: Replace inline test job in `ci.yml`**

Replace the existing `jobs.test` block (lines 13–31) with:

```yaml
jobs:
  test:
    uses: ./.github/workflows/_test.yml

  docker-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - name: Build Docker image
        run: docker build -t data-analyst:test .
  # ... (docker-e2e job unchanged)
```

Leave `docker-build` and `docker-e2e` jobs as-is.

- [ ] **Step 2: Replace inline test job in `release.yml`**

Find the `jobs.test:` block (lines 41–74). Replace with:

```yaml
jobs:
  test:
    # Skip the `create` event for tags — those are owned by keboola-deploy.yml
    # and shouldn't double-build here. Branch creates DO run.
    if: github.event_name != 'create' || github.event.ref_type == 'branch'
    uses: ./.github/workflows/_test.yml
```

`build-and-push: needs: test` already references the `test` job by name, so no change there.

- [ ] **Step 3: Replace inline test job in `keboola-deploy.yml`**

Replace `jobs.test:` block (lines 30–60) with:

```yaml
jobs:
  test:
    uses: ./.github/workflows/_test.yml
```

- [ ] **Step 4: Verify workflow startup parses**

Run: `find .github/workflows -name '*.yml' -exec docker run --rm -v "$(pwd):/repo" rhysd/actionlint:latest -color {} \;`
Expected: no errors.

- [ ] **Step 5: Trigger test runs**

```bash
git push origin chore/release-cleanup
gh run watch  # observe CI on the pushed branch
```

Expected: ci.yml's `test` job runs the reusable workflow successfully. release.yml does NOT trigger image build for this branch yet (Phase 5 will lock that down explicitly; for now the existing `paths-ignore` may skip it).

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/release.yml .github/workflows/keboola-deploy.yml
git commit -m "ci: route ci.yml, release.yml, keboola-deploy.yml through reusable _test.yml

Removes ~80 lines of duplicated install/lint/mypy/pytest steps. Caller workflows now declare the test stage via 'uses: ./.github/workflows/_test.yml' and the recipe lives in one file."
```

---

# Phase 3 — Versioning from git

## Task 5: Switch `pyproject.toml` to `setuptools_scm` dynamic version

**Files:**
- Modify: `pyproject.toml`
- Create: `src/_version.py` (auto-generated, gitignored)
- Modify: `.gitignore`

- [ ] **Step 1: Patch pyproject.toml**

Locate the `[project]` table. Change:

```toml
[project]
name = "agnes-the-ai-analyst"
version = "0.15.0"
```

to:

```toml
[project]
name = "agnes-the-ai-analyst"
dynamic = ["version"]
```

Locate `[build-system]`. Change:

```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"
```

to:

```toml
[build-system]
requires = ["setuptools>=64", "setuptools_scm>=8"]
build-backend = "setuptools.build_meta"
```

Add a new top-level table at the end of pyproject.toml:

```toml
[tool.setuptools_scm]
# Read version from semver release tags only (vX.Y.Z), ignoring CalVer
# build tags (stable-YYYY.MM.N, dev-YYYY.MM.N) and infra-vX.Y.Z module
# tags. Untagged commits get a `0.X.Y.devN+gSHA` post-release suffix.
git_describe_command = ["git", "describe", "--tags", "--match", "v*", "--dirty"]
write_to = "src/_version.py"
fallback_version = "0.0.0+unknown"  # CI builds where shallow checkout returns no tag
```

- [ ] **Step 2: Add generated file to .gitignore**

Append to `.gitignore`:

```
# Auto-generated by setuptools_scm
src/_version.py
```

- [ ] **Step 3: Test locally**

```bash
cd ../tmp_oss-release-cleanup
python3 -m venv .venv-scm-test && source .venv-scm-test/bin/activate
uv pip install --system ".[dev]"
python -c "from importlib.metadata import version; print(version('agnes-the-ai-analyst'))"
```

Expected: prints something matching `<latest-v-tag>` (e.g. `0.18.0`) or `<latest>.devN+g<sha>` if commits exist past the latest `v*` tag. NOT `0.0.0+unknown` and NOT `0.15.0`.

If it prints `0.0.0+unknown`: shallow clone or no `v*` tags reachable. Run `git fetch --tags` and retry.
If it prints `0.15.0`: pyproject.toml edit didn't take effect — verify `dynamic = ["version"]` and no remaining `version = "..."` line.

Deactivate venv: `deactivate && rm -rf .venv-scm-test`

- [ ] **Step 4: Update tests that hardcoded the version**

Run: `grep -rn '"0\.15\.0"\|0\.15\.0' tests/ src/ app/ cli/ docs/ --include='*.py' --include='*.md' | grep -v 'docs/superpowers/'`

For each hit:
- Test files (`tests/**/*.py`): replace literal `"0.15.0"` with a check that the version is non-empty and parseable, or use `importlib.metadata.version("agnes-the-ai-analyst")` if the test is asserting the published version.
- Production code (`src/`, `app/`, `cli/`): if anything reads from a `__version__` constant, replace with `from importlib.metadata import version; __version__ = version("agnes-the-ai-analyst")` or import from the auto-generated `src/_version.py`.

If there are no hits, skip this step.

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v --tb=short -n auto`
Expected: same pass/fail count as before this task.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore
# Plus any test/code changes from Step 4
git commit -m "build: read package version from git tags via setuptools_scm

Drops the hardcoded version = '0.15.0' line that drifted from the
v0.18.0 git tag. setuptools_scm reads from git describe --match 'v*'
so the release tag IS the version. CalVer image tags (stable-YYYY.MM.N)
and infra-vX.Y.Z module tags are filtered out via --match. Generated
src/_version.py is gitignored."
```

---

## Task 6: Drop manual `grep '^version' pyproject.toml` in workflows

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `.github/workflows/keboola-deploy.yml`

- [ ] **Step 1: Replace pkgver step in release.yml**

Find the `Extract package version from pyproject.toml` step in `release.yml` (≈ lines 178–192). Replace with:

```yaml
      - name: Resolve package version from git
        id: pkgver
        run: |
          # Source of truth: git tags via setuptools_scm. Read it the same
          # way the package itself does at runtime so the build arg matches
          # what `da --version` and `/api/version` will report.
          uv pip install --system setuptools_scm
          VERSION=$(python -c "from setuptools_scm import get_version; print(get_version(root='.'))")
          if [ -z "$VERSION" ] || [ "$VERSION" = "0.0.0+unknown" ]; then
            echo "::error::setuptools_scm could not resolve a version (shallow checkout? missing v* tag?)"
            exit 1
          fi
          echo "version=${VERSION}" >> "$GITHUB_OUTPUT"
          echo "Package version: ${VERSION}"
```

(Keep `actions/checkout@v5` with `fetch-depth: 0` and `fetch-tags: true` — already present in the build-and-push job.)

- [ ] **Step 2: Replace pkgver step in keboola-deploy.yml**

Find the `Resolve tag + version` step in `keboola-deploy.yml` (≈ lines 70–89). Replace the `PKG_VERSION` extraction lines with the same setuptools_scm-based block:

```yaml
      - name: Resolve tag + version
        id: meta
        run: |
          TAG="${GITHUB_REF#refs/tags/}"
          case "$TAG" in
            keboola-deploy-*) ;;
            *) echo "::error::Tag $TAG does not match keboola-deploy-* — refusing to build"; exit 1 ;;
          esac
          uv pip install --system setuptools_scm
          PKG_VERSION=$(python -c "from setuptools_scm import get_version; print(get_version(root='.'))")
          if [ -z "$PKG_VERSION" ] || [ "$PKG_VERSION" = "0.0.0+unknown" ]; then
            echo "::error::setuptools_scm could not resolve a version"
            exit 1
          fi
          echo "tag=${TAG}" >> "$GITHUB_OUTPUT"
          echo "pkg_version=${PKG_VERSION}" >> "$GITHUB_OUTPUT"
          echo "Building image for git tag: ${TAG} (package version ${PKG_VERSION})"
```

Add `with: { fetch-depth: 0, fetch-tags: true }` to the `actions/checkout@v5` step in this job if not already present.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml .github/workflows/keboola-deploy.yml
git commit -m "ci: read AGNES_VERSION via setuptools_scm in release + keboola-deploy

Replaces the grep '^version' pyproject.toml hack with the same code path
the runtime uses. After the dynamic-version switch the pyproject line
no longer exists, so the grep would fail silently — this aligns CI
with the new source of truth."
```

---

## Task 7: Smoke-test that `da --version` and `/api/version` report the new version

**Files:**
- Modify: `scripts/smoke-test.sh` (verify version reporting)

- [ ] **Step 1: Read current smoke-test.sh**

Run: `cat scripts/smoke-test.sh`

Locate any existing version assertion. If absent, plan to add one.

- [ ] **Step 2: Add version probe to smoke-test**

Append (or insert near the other authed probes) in `scripts/smoke-test.sh`:

```bash
# Verify the running image reports a sensible version string.
# Should match either a release tag (e.g. 0.18.0) or a dev post-release
# (e.g. 0.18.1.dev3+gabc1234). Empty / 0.0.0 / 'unknown' fails.
echo "Probing /api/version..."
VERSION=$(curl -sf "${BASE_URL}/api/version" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version',''))")
if [ -z "$VERSION" ] || [ "$VERSION" = "0.0.0+unknown" ] || [ "$VERSION" = "unknown" ]; then
  echo "FAIL: /api/version returned '$VERSION' — setuptools_scm did not resolve at build time"
  exit 1
fi
echo "OK: /api/version = $VERSION"
```

If `/api/version` doesn't exist yet, check `app/api/` for a similar endpoint; otherwise add this as a new entry pointing at whatever endpoint surfaces `AGNES_VERSION`.

- [ ] **Step 3: Run smoke-test against the existing main image to baseline**

Run:
```bash
docker run --rm -p 8000:8000 ghcr.io/keboola/agnes-the-ai-analyst:stable &
sleep 10
bash scripts/smoke-test.sh http://localhost:8000
```
Expected: PASS with current `:stable` (which still reports 0.15.0). Stop the container.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke-test.sh
git commit -m "test(smoke): assert /api/version reports a resolved setuptools_scm version

Catches the regression class where AGNES_VERSION build-arg plumbing
breaks (shallow checkout, missing v* tag, scm filter mismatch) and the
running image silently reports '0.0.0+unknown'. CI's smoke-test job
will now fail loudly on that case."
```

---

# Phase 4 — Release notes automation

## Task 8: Add Release Drafter

**Files:**
- Create: `.github/release-drafter.yml`
- Create: `.github/workflows/release-drafter.yml`

- [ ] **Step 1: Author the Release Drafter config**

Create `.github/release-drafter.yml`:

```yaml
# Release Drafter config
# Maintains a rolling draft GitHub Release. On every PR merge to main it
# adds a bullet under the matching category (by PR label or PR-title prefix)
# and bumps the resolved version. When you push a v* tag, publish the draft.

name-template: 'v$RESOLVED_VERSION'
tag-template: 'v$RESOLVED_VERSION'
version-resolver:
  major:
    labels: [breaking, 'BREAKING CHANGE']
  minor:
    labels: [feature, enhancement]
  patch:
    labels: [fix, bug, chore, docs]
  default: patch

categories:
  - title: 'Breaking Changes'
    labels: [breaking, 'BREAKING CHANGE']
  - title: 'Features'
    labels: [feature, enhancement]
  - title: 'Fixes'
    labels: [fix, bug]
  - title: 'Internal'
    labels: [chore, refactor, ci, test, docs]

# Fallback: derive category from conventional-commit prefix in the PR title
# when the PR has no label.
autolabeler:
  - label: breaking
    title: ['/^[a-z]+!:/', '/BREAKING/']
  - label: feature
    title: ['/^feat:/', '/^feat\(.+\):/']
  - label: fix
    title: ['/^fix:/', '/^fix\(.+\):/']
  - label: chore
    title: ['/^chore:/', '/^chore\(.+\):/']
  - label: docs
    title: ['/^docs:/', '/^docs\(.+\):/']
  - label: refactor
    title: ['/^refactor:/']
  - label: ci
    title: ['/^ci:/']
  - label: test
    title: ['/^test:/']

template: |
  ## What's Changed

  $CHANGES

  **Image:** `ghcr.io/keboola/agnes-the-ai-analyst:v$RESOLVED_VERSION`

  **Full Changelog:** https://github.com/keboola/agnes-the-ai-analyst/compare/$PREVIOUS_TAG...v$RESOLVED_VERSION

exclude-labels:
  - skip-changelog

change-template: '- $TITLE (#$NUMBER) by @$AUTHOR'
no-changes-template: '- No user-visible changes'
```

- [ ] **Step 2: Author the workflow that runs Release Drafter**

Create `.github/workflows/release-drafter.yml`:

```yaml
name: Release Drafter

# Updates the draft GitHub Release on every PR merge to main and on
# direct pushes. Operator publishes the draft (manually or via tag push)
# when ready to cut a release.

on:
  push:
    branches: [main]
  pull_request:
    types: [opened, reopened, synchronize, edited]

permissions:
  contents: write       # update draft release
  pull-requests: write  # apply autolabels

jobs:
  update-draft:
    runs-on: ubuntu-latest
    steps:
      - uses: release-drafter/release-drafter@v6
        with:
          config-name: release-drafter.yml
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 3: Validate config syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/release-drafter.yml'))"`
Expected: no output (valid YAML).

- [ ] **Step 4: Commit**

```bash
git add .github/release-drafter.yml .github/workflows/release-drafter.yml
git commit -m "ci: add Release Drafter to maintain rolling draft GitHub Release

Aggregates merged PR titles into a categorized draft release on every
push to main. Categories derive from PR labels or conventional-commit
title prefixes (feat/fix/chore/etc). On vX.Y.Z tag push the operator
publishes the draft, which auto-fills release notes for the cut
release.

Requires PR titles to read well as release-note bullets — captured in
CLAUDE.md release-notes section."
```

- [ ] **Step 5: Verify after merge to main**

After this PR merges, check `https://github.com/keboola/agnes-the-ai-analyst/releases` — the topmost entry should be a draft titled `v<next-version>` containing this PR's title under "Internal".

If no draft appears: check workflow run logs for permission errors. The default `GITHUB_TOKEN` permissions in repo Settings → Actions may need `Read and write permissions`.

---

# Phase 5 — Image pipeline simplification (HIGH-IMPACT)

> **STOP** before starting Phase 5. The changes here affect anyone with a dev VM pinned to `:dev-<prefix>-latest`. Confirm with the team that the migration described in Task 9 is acceptable before continuing.

## Task 9: Drop per-branch builds — `release.yml` publishes only on push-to-main

**Files:**
- Modify: `.github/workflows/release.yml`
- Create: `docs/release-process.md`

- [ ] **Step 1: Author migration guide**

Create `docs/release-process.md`:

```markdown
# Release & deploy reference

## Image tags

| Tag | Trigger | Mutability | Use case |
|-----|---------|------------|----------|
| `:stable` | push to main | floating | production VMs that follow main |
| `:stable-<run-number>` | push to main | immutable | pin to a known-good build |
| `:sha-<7>` | push to main | immutable | git-SHA-pinned debugging |
| `:keboola-deploy-<suffix>` | `keboola-deploy-*` git tag | immutable | shared dev VM with explicit operator-controlled deploys |
| `:keboola-deploy-latest` | `keboola-deploy-*` git tag | floating | the same shared dev VM (auto-pull alias) |
| `:vX.Y.Z` | `vX.Y.Z` git tag | immutable | semver-pinned deploy |

Per-branch dev images (`:dev-<slug>` / `:dev-<prefix>-latest`) were removed on 2026-04-29. To deploy non-main code:

- **Preferred**: push your branch + run `gh workflow run release.yml -r <branch>`. The job publishes `:dev-<sha>-<short-sha>` for explicit pinning. No floating `<prefix>-latest` alias.
- **Quick-and-dirty**: pin your VM to `:stable` and merge to main.

## Cutting a release

1. Verify the Release Drafter draft on the Releases page reflects what shipped.
2. Push the matching tag: `git tag v0.X.Y && git push origin v0.X.Y`.
3. Edit the draft (surface breaking changes / migration steps) and Publish.
4. setuptools_scm picks up the tag; the next build of `:stable` reports `v0.X.Y` as its version.

## Rollback

See `.github/workflows/rollback.yml`. Trigger via `gh workflow run rollback.yml -f stable_target=stable-<run-number>` or it auto-runs when smoke-test fails on a fresh `:stable` push.
```

- [ ] **Step 2: Trim `release.yml` triggers**

In `.github/workflows/release.yml`, replace the `on:` block (lines 3–22) with:

```yaml
on:
  push:
    branches: [main]
    paths-ignore:
      - "docs/**"
      - "*.md"
      - "LICENSE"
  workflow_dispatch:
    inputs:
      reason:
        description: 'Why a manual run? (free text, logged in build summary)'
        required: false
        default: ''
```

This removes:
- `branches: ["**"]`
- `create:` event (was a workaround for paths-ignore on fresh branches — not needed when only main publishes)
- The branch-aware `concurrency` cancel-in-progress is still useful but can be simplified.

- [ ] **Step 3: Trim the channel/branch-slug logic in build-and-push**

Replace the `Claim version tag (with retry to avoid race conditions)` step (lines 101–172) with:

```yaml
      - name: Resolve image identity
        id: meta
        run: |
          # On main: stable. On manual workflow_dispatch from any branch: dev.
          if [[ "${{ github.ref }}" == "refs/heads/main" ]]; then
            CHANNEL="stable"
          else
            CHANNEL="dev"
          fi
          # Use the GH-provided run_number as the per-channel sequence id.
          # Monotonic per repo, no race, no git-tag side-effect.
          RUN_NUM="${{ github.run_number }}"
          SHORT_SHA=$(echo "${{ github.sha }}" | cut -c1-7)
          IMAGE_TAG="${CHANNEL}-${RUN_NUM}"

          echo "channel=${CHANNEL}" >> "$GITHUB_OUTPUT"
          echo "run_number=${RUN_NUM}" >> "$GITHUB_OUTPUT"
          echo "image_tag=${IMAGE_TAG}" >> "$GITHUB_OUTPUT"
          echo "short_sha=${SHORT_SHA}" >> "$GITHUB_OUTPUT"
          echo "Channel: ${CHANNEL}"
          echo "Image tag: ${IMAGE_TAG}"
```

- [ ] **Step 4: Trim the docker build-push tags list**

Replace the `tags:` block in the `Build and push` step (lines 210–215) with:

```yaml
          tags: |
            ghcr.io/${{ github.repository }}:${{ steps.meta.outputs.channel }}
            ghcr.io/${{ github.repository }}:${{ steps.meta.outputs.image_tag }}
            ghcr.io/${{ github.repository }}:sha-${{ steps.meta.outputs.short_sha }}
```

This drops `:dev-<branch-slug>` and `:dev-<prefix>-latest` aliases.

- [ ] **Step 5: Update build-arg references**

In the same `Build and push` step, the `AGNES_TAG=${{ steps.meta.outputs.versioned_tag }}` line referenced an output that no longer exists. Replace `versioned_tag` with `image_tag`:

```yaml
          build-args: |
            AGNES_VERSION=${{ steps.pkgver.outputs.version }}
            RELEASE_CHANNEL=${{ steps.meta.outputs.channel }}
            AGNES_COMMIT_SHA=${{ github.sha }}
            AGNES_TAG=${{ steps.meta.outputs.image_tag }}
```

Also update the `outputs:` block at the top of `build-and-push:` to reflect the renamed output.

- [ ] **Step 6: Update downstream `needs.build-and-push.outputs.image_tag` references**

In `smoke-test:` and `e2e-bind-mount:` jobs, the references should already use `image_tag`. Grep for `versioned_tag` to confirm none remain:

Run: `grep -n versioned_tag .github/workflows/release.yml`
Expected: zero hits. Fix any that remain.

- [ ] **Step 7: Validate**

Run: `docker run --rm -v "$(pwd):/repo" rhysd/actionlint:latest -color /repo/.github/workflows/release.yml`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/release.yml docs/release-process.md
git commit -m "ci: drop per-branch image builds in release.yml; explicit policy

The previous policy built an image for every push to every branch and
published per-developer floating aliases (:dev-<prefix>-latest). This
was a footgun for shared dev VMs (last pusher wins) and required
~150 lines of branch-slug parsing, Git-Flow skip-list, paths-ignore
+ create-event workarounds, and a 5-attempt git-tag race loop.

New policy: release.yml builds on push to main only (:stable +
:stable-<run-number> + :sha-<7>). Non-main builds are explicit via
workflow_dispatch. Operator-facing migration documented in
docs/release-process.md."
```

---

## Task 10: Drop CalVer claim-tag race loop (already mostly removed by Task 9)

**Status:** Mostly subsumed by Task 9 (the loop was removed when `Resolve image identity` replaced `Claim version tag`). This task verifies cleanup and removes residual state.

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Confirm no `git tag` calls remain in release.yml**

Run: `grep -n 'git tag' .github/workflows/release.yml`
Expected: zero hits.

If any remain, identify whether they're load-bearing (auto-tagging the commit with the CalVer version was a side-effect of the old loop — nothing else depends on those tags now that setuptools_scm filters them out via `--match 'v*'`).

- [ ] **Step 2: Confirm no `git fetch --tags --force` calls remain**

Run: `grep -n 'fetch --tags' .github/workflows/release.yml`
Expected: zero hits in the tag-claim path. The `fetch-tags: true` on `actions/checkout@v5` for setuptools_scm is the legitimate use.

- [ ] **Step 3: No commit needed if no further changes**

If Task 9's commit already cleaned this up, skip the commit step. Otherwise commit any residual cleanup as `ci: remove residual CalVer tag-claim plumbing`.

---

## Task 11: Extract auto-rollback to `rollback.yml`

**Files:**
- Create: `.github/workflows/rollback.yml`
- Modify: `.github/workflows/release.yml` (smoke-test job)

- [ ] **Step 1: Author rollback.yml**

Create `.github/workflows/rollback.yml`:

```yaml
name: Rollback :stable

# Re-tag :stable to a previous known-good build, deprecate the failing
# image, and open a tracking issue. Callable from release.yml on
# smoke-test failure (workflow_call) or manually by an operator
# (workflow_dispatch) when something breaks post-deploy.

on:
  workflow_call:
    inputs:
      failed_image_tag:
        description: 'The image_tag that failed (e.g. stable-12345)'
        type: string
        required: true
      target_image_tag:
        description: 'Override the rollback target. Defaults to the previous stable-* image.'
        type: string
        required: false
  workflow_dispatch:
    inputs:
      failed_image_tag:
        description: 'The image_tag that failed (e.g. stable-12345)'
        type: string
        required: true
      target_image_tag:
        description: 'Rollback target. Defaults to the previous :stable-* run.'
        type: string
        required: false

permissions:
  contents: write
  packages: write
  issues: write

jobs:
  rollback:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - name: Log in to GHCR
        uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Resolve target image
        id: target
        run: |
          REPO="ghcr.io/${{ github.repository }}"
          if [ -n "${{ inputs.target_image_tag }}" ]; then
            TARGET="${{ inputs.target_image_tag }}"
          else
            # Find the run-number immediately preceding the failed one.
            FAILED="${{ inputs.failed_image_tag }}"
            FAILED_NUM=$(echo "$FAILED" | sed -E 's/^stable-([0-9]+)$/\1/')
            if [ -z "$FAILED_NUM" ]; then
              echo "::error::failed_image_tag=$FAILED is not stable-<n> — supply target_image_tag"
              exit 1
            fi
            PREV_NUM=$((FAILED_NUM - 1))
            TARGET="stable-${PREV_NUM}"
          fi
          echo "target=$TARGET" >> "$GITHUB_OUTPUT"
          echo "Rollback target: $TARGET"

      - name: Re-tag :stable to target + mark failed image deprecated
        run: |
          REPO="ghcr.io/${{ github.repository }}"
          FAILED="${{ inputs.failed_image_tag }}"
          TARGET="${{ steps.target.outputs.target }}"

          docker pull "$REPO:$FAILED"
          docker tag "$REPO:$FAILED" "$REPO:deprecated-${FAILED}"
          docker push "$REPO:deprecated-${FAILED}"

          docker pull "$REPO:$TARGET"
          docker tag "$REPO:$TARGET" "$REPO:stable"
          docker push "$REPO:stable"

      - name: Open tracking issue
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh issue create \
            --title "Rollback: :stable reverted from ${{ inputs.failed_image_tag }} to ${{ steps.target.outputs.target }}" \
            --body "$(cat <<EOF
          ## Rollback report

          - Failed image: \`ghcr.io/${{ github.repository }}:${{ inputs.failed_image_tag }}\`
          - Deprecated tag: \`ghcr.io/${{ github.repository }}:deprecated-${{ inputs.failed_image_tag }}\`
          - Rolled back to: \`ghcr.io/${{ github.repository }}:${{ steps.target.outputs.target }}\`
          - Triggered by: ${{ github.event_name }}
          - Run: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}

          Investigate before re-deploying.
          EOF
          )" \
            --label "bug,rollback"
```

- [ ] **Step 2: Replace inline auto-rollback in release.yml smoke-test job**

In `.github/workflows/release.yml`, find the `Automatic rollback on failure` step (lines 240–270 in the pre-cleanup version; line numbers will have shifted). Delete it.

Replace the smoke-test job's failure handling by adding a job-level `outputs.failed: true` set on smoke failure, and a downstream job:

```yaml
  smoke-test:
    needs: build-and-push
    runs-on: ubuntu-latest
    outputs:
      failed: ${{ steps.smoke.outcome == 'failure' }}
    steps:
      # ... existing setup steps ...
      - name: Run smoke tests
        id: smoke
        run: bash scripts/smoke-test.sh http://localhost:8000
      # ... existing log-collect / upload / teardown steps (keep these) ...

  rollback-on-smoke-fail:
    needs: [build-and-push, smoke-test]
    if: failure() && needs.smoke-test.outputs.failed == 'true'
    uses: ./.github/workflows/rollback.yml
    with:
      failed_image_tag: ${{ needs.build-and-push.outputs.image_tag }}
    permissions:
      contents: write
      packages: write
      issues: write
```

- [ ] **Step 3: Validate**

Run: `docker run --rm -v "$(pwd):/repo" rhysd/actionlint:latest -color /repo/.github/workflows/rollback.yml /repo/.github/workflows/release.yml`
Expected: no errors.

- [ ] **Step 4: Test rollback manually against a fake image**

Don't push real :stable yet. Instead, in a scratch repo or by manually pulling and inspecting:

```bash
# Verify the workflow file's logic by reading it back
cat .github/workflows/rollback.yml | grep -A2 'docker tag'
```
Expected: shows the re-tag commands as written.

Real test: after merge to main, manually trigger via:
```bash
gh workflow run rollback.yml -f failed_image_tag=stable-99999 -f target_image_tag=stable-<known-good>
```
…against a non-production tag if you want to dry-run. Otherwise wait for a real failure to exercise it.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/rollback.yml .github/workflows/release.yml
git commit -m "ci: extract auto-rollback into reusable rollback.yml

Pulls the inline Automatic rollback on failure step out of release.yml's
smoke-test job into a dedicated workflow callable via workflow_call
(from release.yml on smoke failure) or workflow_dispatch (manual
operator-triggered rollback after a regression escapes smoke).

Benefits:
- testable: workflow_dispatch lets an operator dry-run against scratch tags
- auditable: separate run history per rollback event
- composable: any future workflow can call it"
```

---

## Task 12: Add scheduled `dev-*` tag prune

**Files:**
- Create: `scripts/ops/prune-dev-tags.sh`
- Create: `.github/workflows/prune-dev-tags.yml`

- [ ] **Step 1: Author prune script**

Create `scripts/ops/prune-dev-tags.sh`:

```bash
#!/usr/bin/env bash
# Prune old dev-YYYY.MM.N git tags + matching GHCR images. Keeps the
# most-recent KEEP_COUNT (default 30) so a developer rolling back a
# week of dev builds still has options. Run via .github/workflows/
# prune-dev-tags.yml on a schedule or via workflow_dispatch.
#
# Idempotent: re-running with no new tags exits cleanly.
# Dry-run: PRUNE_DRY_RUN=1 prints what would be deleted without acting.

set -euo pipefail

KEEP_COUNT="${KEEP_COUNT:-30}"
DRY_RUN="${PRUNE_DRY_RUN:-0}"
REPO="${GITHUB_REPOSITORY:?must be set}"

cd "$(git rev-parse --show-toplevel)"

# Tags matching dev-YYYY.MM.N or stable-YYYY.MM.N (legacy CalVer scheme).
# Sort by version (newest first), drop the top KEEP_COUNT, prune the rest.
LEGACY_TAGS=$(git tag -l 'dev-*' 'stable-*' | grep -E '^(dev|stable)-[0-9]{4}\.[0-9]{2}\.[0-9]+$' | sort -t. -k1,1 -k2,2 -k3,3n -r)
TO_PRUNE=$(echo "$LEGACY_TAGS" | tail -n +"$((KEEP_COUNT + 1))" | head -200)  # cap per-run for safety

if [ -z "$TO_PRUNE" ]; then
  echo "Nothing to prune (have $(echo "$LEGACY_TAGS" | wc -l) legacy tags, keep ${KEEP_COUNT})"
  exit 0
fi

echo "Will prune $(echo "$TO_PRUNE" | wc -l) legacy tags (keeping ${KEEP_COUNT} newest)..."
if [ "$DRY_RUN" = "1" ]; then
  echo "$TO_PRUNE" | head -10
  echo "(dry-run — no deletions)"
  exit 0
fi

# Delete git tags (remote first; local is harmless if it fails)
echo "$TO_PRUNE" | while read -r TAG; do
  [ -z "$TAG" ] && continue
  echo "  deleting tag: $TAG"
  git push origin --delete "$TAG" 2>/dev/null || echo "    (already gone on remote)"
  git tag -d "$TAG" 2>/dev/null || true
done

# Delete GHCR images. Requires PACKAGE_TOKEN with packages:delete on the org.
if [ -n "${GH_TOKEN:-}" ]; then
  PKG_NAME=$(echo "$REPO" | cut -d/ -f2)
  echo "$TO_PRUNE" | while read -r TAG; do
    [ -z "$TAG" ] && continue
    VERSION_ID=$(gh api "/orgs/$(echo "$REPO" | cut -d/ -f1)/packages/container/${PKG_NAME}/versions" \
      --paginate --jq ".[] | select(.metadata.container.tags[] | . == \"$TAG\") | .id" | head -1 || true)
    if [ -n "$VERSION_ID" ]; then
      echo "  deleting GHCR image $TAG (version $VERSION_ID)"
      gh api -X DELETE "/orgs/$(echo "$REPO" | cut -d/ -f1)/packages/container/${PKG_NAME}/versions/${VERSION_ID}" || \
        echo "    (failed; may be pinned to :stable, leaving)"
    fi
  done
else
  echo "GH_TOKEN unset — skipping GHCR image deletion (git tags were pruned)"
fi
```

Make it executable: `chmod +x scripts/ops/prune-dev-tags.sh`

- [ ] **Step 2: Self-test (dry-run)**

```bash
PRUNE_DRY_RUN=1 GITHUB_REPOSITORY=keboola/agnes-the-ai-analyst \
  bash scripts/ops/prune-dev-tags.sh | head -20
```

Expected: prints "Will prune NNN legacy tags...", lists 10 sample tags, says "(dry-run — no deletions)". Exit code 0. No tags actually deleted (verify with `git tag -l 'dev-*' | wc -l` before/after).

- [ ] **Step 3: Author the workflow wrapper**

Create `.github/workflows/prune-dev-tags.yml`:

```yaml
name: Prune dev tags

# Weekly: prune dev-YYYY.MM.N and stale stable-YYYY.MM.N legacy tags.
# Keeps the 30 most recent of each. Manual trigger supports dry-run via
# the dry_run input.

on:
  schedule:
    - cron: '0 4 * * 0'  # Sundays 04:00 UTC
  workflow_dispatch:
    inputs:
      dry_run:
        description: 'Dry-run only — list tags that would be pruned, do not delete'
        type: boolean
        default: true
      keep_count:
        description: 'Number of newest legacy tags to keep'
        type: string
        default: '30'

permissions:
  contents: write   # delete git tags
  packages: write   # delete GHCR images

jobs:
  prune:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
          fetch-tags: true

      - name: Run prune
        env:
          GITHUB_REPOSITORY: ${{ github.repository }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          KEEP_COUNT: ${{ inputs.keep_count || '30' }}
          PRUNE_DRY_RUN: ${{ inputs.dry_run && '1' || '0' }}
        run: bash scripts/ops/prune-dev-tags.sh
```

- [ ] **Step 4: Commit**

```bash
git add scripts/ops/prune-dev-tags.sh .github/workflows/prune-dev-tags.yml
git commit -m "ci: prune dev-YYYY.MM.N legacy tags + GHCR images weekly

The old per-push CalVer scheme accumulated 475 dev-* tags in April
alone (475 git refs, 475 GHCR image versions, ~2GB of registry storage
per month at typical layer sizes). New scheme uses github.run_number
so this is bounded going forward, but the legacy tags need a one-time
prune + ongoing weekly housekeeping in case any operator pushes a
:dev tag manually. Defaults: keep newest 30, dry-run via workflow_dispatch."
```

---

# Phase 6 — Documentation pass

## Task 13: Rewrite "Release & deploy workflows" section in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read the current section**

Run: `awk '/^## Release & deploy workflows/,/^## /' CLAUDE.md | head -50`
Expected: shows the section that describes release.yml + keboola-deploy.yml.

- [ ] **Step 2: Replace the "Release & deploy workflows" section**

Replace lines from `## Release & deploy workflows` through (and excluding) the next `## ` heading with:

```markdown
## Release & deploy workflows

Three GitHub Actions workflows publish images to GHCR. They share `_test.yml` for the test stage.

### `release.yml` — main-only auto-build
Runs on every push to `main` (and on `workflow_dispatch` for any branch). Publishes:
- `:stable` — floating, the latest main build
- `:stable-<run-number>` — immutable, pinnable
- `:sha-<7>` — git-SHA-pinned debugging tag

Per-branch dev images were dropped on 2026-04-29 — see `docs/release-process.md` for the operator-facing deploy reference and migration. Anyone needing an image off a non-main branch uses `workflow_dispatch` (publishes `:dev` + `:dev-<run-number>` + `:sha-<7>`).

Smoke test on the freshly-built `:stable` runs `scripts/smoke-test.sh`. On failure it triggers `rollback.yml` which re-tags `:stable` to the previous `stable-<run-number>` and opens a tracking issue.

### `keboola-deploy.yml` — tag-triggered, explicit deploy
Runs only on `keboola-deploy-*` git tag pushes. Publishes:
- `:keboola-deploy-<git-tag-suffix>` — immutable
- `:keboola-deploy-latest` — floating alias

Operator workflow is unchanged — see `docs/release-process.md`.

### `_test.yml` — reusable test stage
Called by `release.yml`, `keboola-deploy.yml`, and `ci.yml` via `uses: ./.github/workflows/_test.yml`. One copy of the install/lint/mypy/pytest recipe.

### Versioning
Package version is read from git tags via `setuptools_scm` (filtered to `v*` semver tags). To cut a release: push a `vX.Y.Z` tag, publish the Release Drafter draft on the Releases page. The next `:stable` build's `/api/version` reports `X.Y.Z`.

### Module versioning
The customer-instance Terraform module under `infra/modules/customer-instance/` is published as `infra-vMAJOR.MINOR.PATCH` git tags. Bump on any module-API change; downstream infra repos pin to the tag in their `source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.X.Y"`.

After merging a module change to `main`:

    git tag infra-vX.Y.Z origin/main
    git push origin infra-vX.Y.Z

### Replacing a VM after a startup-script change
Module sets `lifecycle { ignore_changes = [metadata_startup_script] }` on `google_compute_instance.vm` so normal `terraform apply` doesn't churn running VMs. To propagate a startup-script update, trigger the consumer's apply workflow manually with the VM resource address — typical workflow_dispatch input is `recreate_targets='module.agnes.google_compute_instance.vm["<vm-name>"]'`.
```

- [ ] **Step 3: Verify no dangling references**

Run: `grep -n 'CalVer\|dev-<prefix>\|claim version\|paths-ignore' CLAUDE.md`
Expected: zero hits (those concepts are gone post-cleanup).

Run: `grep -n 'CHANGELOG' CLAUDE.md`
Expected: zero hits (deleted in Task 1).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: rewrite Release & deploy workflows in CLAUDE.md

Reflects the post-cleanup pipeline shape: main-only auto-builds,
explicit workflow_dispatch for non-main, setuptools_scm versioning,
Release Drafter for notes, rollback as a separate workflow, prune
job for legacy tags. Removes references to CalVer claim-tag race,
per-branch builds, and dev-<prefix>-latest aliases — all gone."
```

---

# Final verification checklist

After all tasks complete, run this end-to-end before opening PR:

- [ ] `git status --short` clean (everything committed)
- [ ] `find .github/workflows -name '*.yml' -exec docker run --rm -v "$(pwd):/repo" rhysd/actionlint:latest -color {} \;` — zero errors
- [ ] `pytest tests/ -v --tb=short -n auto` — same pass count as on main
- [ ] `python -c "from setuptools_scm import get_version; print(get_version(root='.'))"` — prints something matching `^[0-9]+\.[0-9]+\.[0-9]+`
- [ ] `grep -rn 'CHANGELOG\.md' --include='*.py' --include='*.yml' --include='*.toml' --include='*.sh' . | grep -v 'docs/superpowers/'` — empty
- [ ] `grep -rn 'versioned_tag\|claim version' .github/workflows/` — empty
- [ ] PR description summarizes the operator-facing migration in 5 bullets max + links `docs/release-process.md`

---

# Open questions to resolve before execution

These are decisions that affect the plan but should be confirmed before starting Phase 5:

1. **Per-branch build migration window**: any team member currently pinned to `:dev-<prefix>-latest` on a personal VM? If yes, give them lead-time to re-pin to `:stable` or set up a `workflow_dispatch` shortcut.
2. **Rollback target heuristic**: `stable-<run-number-1>` assumes the previous run was healthy. If the previous run was a fluke skip (e.g. paths-ignore), rollback might pick an even older image. Acceptable, or do we want to scan back for the last `smoke-test.outcome == 'success'`? Acceptable for v1; revisit if it bites.
3. **Tag prune blast radius**: 475 dev-* tags pruned in one go is fine for git, but for GHCR each `gh api DELETE` is a separate request. With 30-tag keep + 200/run cap and weekly cadence, we burn through the backlog in ~3 weeks. OK or want a one-shot bigger run first?
4. **`infra-v*` and `keboola-deploy-*` tags**: explicitly excluded from prune via grep filter — verify before first run that no `infra-v*` tags accidentally match `(dev|stable)-YYYY.MM.N` (they don't, but assert in the script comment).

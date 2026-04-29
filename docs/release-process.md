# Release & deploy reference

Operator-facing reference for how images are built, published, and rolled back. The CI side of this lives in `.github/workflows/release.yml` and `.github/workflows/keboola-deploy.yml`.

## Image tags

| Tag | Trigger | Mutability | Use case |
|---|---|---|---|
| `:stable` | push to `main` | floating | production VMs that follow `main` |
| `:stable-<run-number>` | push to `main` | immutable | pin to a known-good build |
| `:dev` | `workflow_dispatch` from non-main | floating | latest dev build across all branches |
| `:dev-<run-number>` | `workflow_dispatch` from non-main | immutable | pin to a specific dev build |
| `:dev-<branch-slug>` | `workflow_dispatch` from non-main | rolling | latest build of that specific branch |
| `:dev-<prefix>-latest` | `workflow_dispatch` from `<prefix>/<rest>` | floating | per-developer alias (e.g. `:dev-zs-latest` from `zs/foo`); skipped for Git Flow prefixes (`feature/*`, `fix/*`, etc.) |
| `:sha-<7>` | any build | immutable | git-SHA-pinned debugging |
| `:keboola-deploy-<suffix>` | `keboola-deploy-*` git tag | immutable | shared dev VM with explicit operator-controlled deploys |
| `:keboola-deploy-latest` | `keboola-deploy-*` git tag | floating | the same shared dev VM (auto-pull alias) |
| `:vX.Y.Z` | (future) | immutable | semver-pinned deploy when Release Drafter publishes |

`<run-number>` is `${{ github.run_number }}` — monotonic per-repo, no race, no git-tag side-effect on the repo.

## Builds run on

- **Push to `main`** (auto) — publishes `:stable`, `:stable-<run>`, `:sha-<7>`. Smoke-test + e2e-bind-mount run on the just-built image; on smoke failure, `:stable` rolls back to the previous build (Task 11 will extract this to `rollback.yml`).
- **Manual `gh workflow run release.yml -r <branch>`** — publishes `:dev`, `:dev-<run>`, `:dev-<branch-slug>`, `:dev-<prefix>-latest` (when prefix isn't a Git Flow keyword), `:sha-<7>`. Smoke + bind-mount jobs are skipped (gated to main only).
- **`keboola-deploy-*` tag push** — handled by `keboola-deploy.yml`, not `release.yml`.

Pushes to non-main branches do NOT auto-build images. To deploy non-main code:

```bash
gh workflow run release.yml -r zs/foo --field reason="testing fix for #123"
```

The build inherits all the per-branch tag aliases (so a VM pinned to `:dev-zs-latest` will auto-pull on the next 5-minute cron). The optional `reason` input is logged in the workflow run summary.

## Cutting a release

1. Verify the [Release Drafter](https://github.com/keboola/agnes-the-ai-analyst/releases) draft reflects what shipped since the last `v*` tag.
2. Push the matching tag:
   ```bash
   git tag v0.X.Y origin/main
   git push origin v0.X.Y
   ```
3. Edit the draft on the Releases page (surface breaking changes / migration steps at the top) and click **Publish release**.
4. `setuptools_scm` picks up the new tag; the next `:stable` build's `/api/version` reports `0.X.Y`.

## Rollback

Auto-rollback fires if the smoke-test job on a fresh `:stable` push fails. Currently inline in `release.yml`'s smoke-test job; Task 11 of the release-cleanup plan extracts it into `.github/workflows/rollback.yml` so it's also manually triggerable:

```bash
# Manual rollback (after Task 11 lands)
gh workflow run rollback.yml -f failed_image_tag=stable-475
# or with explicit target
gh workflow run rollback.yml -f failed_image_tag=stable-475 -f target_image_tag=stable-470
```

Rollback effects:
- Pulls the failed image, re-tags as `:deprecated-<failed-tag>`, pushes (forensics)
- Pulls the target image, re-tags as `:stable`, pushes
- Opens a GitHub Issue titled `Rollback: :stable reverted from X to Y` labeled `bug,rollback` — primary operator notification

## Tag housekeeping

`scripts/ops/prune-dev-tags.sh` (Task 12) runs weekly via `.github/workflows/prune-dev-tags.yml` and prunes legacy `dev-YYYY.MM.N` / `stable-YYYY.MM.N` git tags + GHCR image versions. Default retention: current + previous month. Manual trigger supports dry-run.

The new `<channel>-<run-number>` scheme (e.g. `stable-475`) is NOT pruned by this job — image versions are kept until the operator explicitly removes them.

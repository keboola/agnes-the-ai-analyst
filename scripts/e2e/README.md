# Agent-browser E2E smoke scripts

Browser-driven smoke tests that catch regressions the pytest suite
can't (rendering, JS errors, click-through flows). Run nightly in CI
via [`.github/workflows/e2e-nightly.yml`](../../.github/workflows/e2e-nightly.yml);
on failure the workflow opens a tracking issue labeled
`agent-browser-nightly`.

## Local development

```bash
# Install the CLI globally once.
npm i -g agent-browser
agent-browser install              # downloads Chrome

# Spin up agnes locally (any of the docs/DEPLOYMENT.md options work).
docker compose up -d

# Run a smoke script.
bash scripts/e2e/smoke_catalog.sh http://localhost:8000
```

Each script:

- Takes the base URL as the only argument (defaults to `http://localhost:8000`).
- Exits non-zero on any failure (the CI job converts that into a
  GitHub issue automatically).
- Stores screenshots into `$ARTIFACTS_DIR` if set; the nightly
  workflow uploads them as build artifacts.

## Adding a new smoke script

Add `smoke_<area>.sh` and append its filename to the `script:` matrix
in the workflow so the new check runs in its own job (parallel + isolated
failure reporting). Keep each script under ~30s of wall-clock —
nightly slot is 5 minutes total per matrix entry.

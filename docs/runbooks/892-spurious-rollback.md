# Spurious `:stable` rollback from a transient registry error (#892)

## Symptom

`release.yml` auto-rolled `:stable` back and opened a tracking issue, e.g. #892:

- Failed image: `stable-2026.07.525` (commit `a793f21d`, #890)
- Rolled back to: `stable-2026.07.520`
- Trigger: push to `main`

## Root cause

**Not a code defect.** The `smoke-test` job failed on its **first attempt** while
pulling the freshly-built image from GHCR:

```
Error response from daemon: Get "https://ghcr.io/v2/":
  net/http: request canceled (Client.Timeout exceeded while awaiting headers)
Process completed with exit code 1
```

The smoke assertions never ran — the job died fetching the image. Because
`rollback-on-smoke-fail` fires on `needs.smoke-test.result == 'failure'`, a
transient ghcr.io network blip was indistinguishable from a genuinely broken
`:stable`, so it rolled prod back and filed the issue.

**Evidence it was transient:** a re-run (attempt 2) of the *exact same commit*
`a793f21d` passed smoke cleanly, the change is still on `main` unchanged, and
`main` has cut many healthy `stable-*` releases since. So `stable-2026.07.525`
was fine; only the smoke-test's image pull flaked.

## Fix

`release.yml` `smoke-test` job now retries the two registry operations with
backoff (5 attempts, `5·attempt` s):

- **GHCR login** — replaced `docker/login-action` with a retried
  `docker login --password-stdin` loop.
- **App image pull** — added an explicit retried `docker compose pull app`
  before `up -d`, so `up` starts from an already-pulled local image.

A real broken build still fails smoke (the assertions in `scripts/smoke-test.sh`
run once the image is up) and still rolls back — only pure registry-fetch flakes
are now absorbed.

## If it happens again

1. Open the rollback run → `smoke-test` job (attempt 1) logs.
2. If the failure is a registry/network error **before** `scripts/smoke-test.sh`
   runs (login/pull timeout, 5xx from ghcr.io), it's the same class of flake:
   re-run the release job; the commit is almost certainly fine.
3. If `scripts/smoke-test.sh` itself reports failing assertions, treat it as a
   real regression — investigate the commit before re-deploying.

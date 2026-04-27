# Local Development

Single source of truth for working on Agnes against `localhost`. Covers the dev-mode auth bypass, mocked Google Workspace groups, what isn't mocked, and the safety rails that keep the dev shortcuts off production.

## TL;DR

```bash
make local-dev
```

Then open <http://localhost:8000>. You land on `/dashboard` already logged in as `dev@localhost` (role `admin`) and your `/profile` shows two mocked Workspace groups. No login screen, no `.env` file, no SMTP, no GCP project — just code.

On Windows (or anywhere GNU Make / bash aren't available), `scripts\run-local-dev.ps1` is the feature-equivalent sibling — same compose stack, same `LOCAL_DEV_GROUPS` default. Verified on Docker Desktop for Windows.

```powershell
.\scripts\run-local-dev.ps1            # up — reuses existing image (auto-builds first run)
.\scripts\run-local-dev.ps1 -Build     # up --build — after pyproject.toml / Dockerfile changes
.\scripts\run-local-dev.ps1 down       # stop + remove containers (data volume preserved)
.\scripts\run-local-dev.ps1 logs       # tail logs
```

What `make local-dev` actually does:

- Stacks three Compose files: `docker-compose.yml` (base) + `docker-compose.override.yml` (hot-reload + source bind mount) + `docker-compose.local-dev.yml` (LOCAL_DEV_MODE overlay).
- Seeds `LOCAL_DEV_GROUPS` with a sensible default (engineers + admins on `example.com`) so `/profile` is non-empty on first boot.
- Touches an empty `.env` if missing — Compose validates `env_file:` paths even for services that never start, and the local-dev overlay drops the env-file requirement for the services that do.

`make local-dev-down` stops the stack; `make local-dev-logs` tails it.

## What `LOCAL_DEV_MODE=1` actually bypasses

The local-dev overlay sets `LOCAL_DEV_MODE=1`, which flips four switches:

1. **Auth bypass.** `app/auth/dependencies.py::get_current_user` short-circuits to a seeded admin user (`dev@localhost` by default; override via `LOCAL_DEV_USER_EMAIL`) before any token check runs. Every protected route — REST and HTML — auto-authenticates.
2. **Magic-link emails skip SMTP.** When the email-link auth provider is exercised in dev, the link is logged to stderr and returned in the response body instead of sent over wire. No mail server, no inbox.
3. **Secrets self-seed.** `JWT_SECRET_KEY` and `SESSION_SECRET` auto-generate into `/data/state/` on first boot if not provided. You don't need to manage them manually.
4. **No `.env` requirement.** The overlay declares `env_file: []` on the affected services, so the project-level `.env` doesn't need to exist. Everything dev-relevant is inline in `docker-compose.local-dev.yml`.

A loud warning banner is logged at startup when `LOCAL_DEV_MODE=1`:

```
============================================================
LOCAL_DEV_MODE is ON — authentication is bypassed.
All requests auto-authenticate as: dev@localhost
LOCAL_DEV_GROUPS: mocking 2 group(s) into session: local-dev-engineers@example.com, local-dev-admins@example.com
NEVER enable this in a deployment reachable from the internet.
============================================================
```

If you don't see that banner at boot, dev mode isn't on — check `LOCAL_DEV_MODE=1` made it into the container's env.

## Mocking Google Workspace groups

`/profile` and any future group-aware code path read `session.google_groups`. In production that field gets populated by the OAuth callback (`app/auth/providers/google.py`) from a Cloud Identity `searchTransitiveGroups` call. In dev there's no OAuth round-trip, so the field stays empty unless we mock it.

`LOCAL_DEV_GROUPS` is a JSON array of objects matching the production shape:

```bash
export LOCAL_DEV_GROUPS='[{"id":"engineers@example.com","name":"Engineering"},{"id":"admins@example.com","name":"Admins"}]'
```

The values flow into `session.google_groups` on every dev-bypass request, so group-aware code sees something realistic. Same `{id, name}` shape the OAuth callback writes.

### How `make local-dev` seeds it

`scripts/run-local-dev.sh` sets a default if you haven't already (engineers + admins on `example.com`), so first-boot is non-empty. Three ways to control it:

```bash
make local-dev                                          # default mock — engineers + admins
LOCAL_DEV_GROUPS='[{"id":"qa@x.com","name":"QA"}]' make local-dev   # custom mock
LOCAL_DEV_GROUPS= make local-dev                         # empty — exercise the no-groups path
```

### Verifying the mock

Two checks:

1. **Boot banner** logs the parsed group IDs (or warns loudly if the JSON is malformed):
    ```
    LOCAL_DEV_GROUPS: mocking 2 group(s) into session: local-dev-engineers@example.com, local-dev-admins@example.com
    ```
    A typo (e.g. unbalanced bracket) shows up here — not silently on the first authenticated request.

2. **`/profile`** renders the mocked groups in a list. If you set `LOCAL_DEV_GROUPS=` (empty), you'll see *"No Google groups available"*.

### Edge case: clearing stale groups mid-session

If you previously had `LOCAL_DEV_GROUPS` set, then unset it and made a request, the dev-bypass path now writes `[]` into the session — same semantics as the production OAuth callback, which always rewrites `session.google_groups` on each login. You won't get stuck looking at stale mocked groups after toggling the env var.

## What's NOT mocked

`LOCAL_DEV_MODE` is intentionally narrow. These still need real configuration if you exercise them:

- **Cloud Identity API.** No real call ever fires in dev. `LOCAL_DEV_GROUPS` populates `session.google_groups` directly without going through `_fetch_google_groups`. To debug the actual API call, use `scripts/debug/probe_google_groups.py` against a real OAuth token.
- **Real OAuth round-trip.** Google login button is hidden / no-op in dev mode. To test the full OAuth flow, follow `docs/auth-google-oauth.md` and unset `LOCAL_DEV_MODE`.
- **Admin Workspace permissions.** The mocked groups are not authoritative — they live only in your browser session. They don't grant any real access to anything outside Agnes; they let you exercise group-aware code paths inside the app.
- **PAT (Personal Access Token) flow.** PATs work normally in dev mode; the dev bypass only short-circuits cookie/session auth. Token-bearer requests still hit the JWT validation path.

## Security model

`LOCAL_DEV_MODE=1` is a footgun by design — every protected route auto-authenticates as admin without any check. The codebase has these rails to keep it from leaking into prod:

- **`docker-compose.local-dev.yml` is a separate overlay**, never stacked into `docker-compose.prod.yml`. Production deployments never see it.
- **The startup banner is loud and unmissable** — `WARNING` level, repeated 60-character separator. Anyone reading container logs at startup will spot it immediately.
- **`is_local_dev_mode()` reads `os.environ` fresh on every call** — no startup-time cache that could be poisoned.
- **`LOCAL_DEV_GROUPS` is honored only inside the `if is_local_dev_mode():` block** in `get_current_user`. Setting it without `LOCAL_DEV_MODE=1` does nothing.

If you ever see the dev banner in a real deployment's logs, treat it as a P0 incident: the auth boundary is gone.

## Cross-links

- [`docs/auth-groups.md`](auth-groups.md) — production Google Workspace groups: GCP setup checklist, the `security` label gotcha, debugging the real Cloud Identity call.
- [`docs/auth-google-oauth.md`](auth-google-oauth.md) — full Google OAuth setup for non-dev environments (client ID, scopes, redirect URIs).
- [`docs/QUICKSTART.md`](QUICKSTART.md) — first-time setup for a real (non-dev) instance.
- [`CLAUDE.md`](../CLAUDE.md) — repo-wide engineering conventions (changelog discipline, vendor-agnostic OSS rules, project structure).

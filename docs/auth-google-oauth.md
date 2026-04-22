# Google OAuth — operator gotchas

The Google OAuth provider (`app/auth/providers/google.py`) reads `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` straight from environment variables. If either is empty, `is_available()` returns `False` and the login page falls back to email / password auth without complaint.

## Env vars

| Var | Required for Google | Notes |
|---|---|---|
| `GOOGLE_CLIENT_ID` | yes | From Google Cloud Console OAuth 2.0 Client ID (Web application). |
| `GOOGLE_CLIENT_SECRET` | yes | From the same client. Rotate via "Reset secret" on the client; old value is invalidated immediately. |
| `SESSION_SECRET` | yes | Used by Starlette `SessionMiddleware` to stash OAuth `state`/`nonce` between `/auth/google/login` and `/auth/google/callback`. Auto-generated to `data/state/.session_secret` if unset, but for multi-replica or VM-rebuild scenarios pin it explicitly. |
| `JWT_SECRET_KEY` | yes | Signs the access-token cookie. Same auto-generate-and-persist pattern as `SESSION_SECRET`. |
| `FORWARDED_ALLOW_IPS` | only when behind a reverse proxy | Default `127.0.0.1` — uvicorn ignores `X-Forwarded-Proto/Host` from any other client IP, which means callbacks come back as `http://localhost:8000/...` instead of `https://your-host/...`. Set to `*` (or the proxy's IP) when terminating TLS at Caddy / nginx / Cloudflare Tunnel. The compose `command:` already passes `--proxy-headers --forwarded-allow-ips '*'` — this env var is the override. |
| `SEED_ADMIN_EMAIL` | recommended on first boot | App startup (`app/main.py`) creates this user with `role="admin"` if missing. Combined with Google OAuth, the first time the matching email signs in, `repo.get_by_email()` finds the seeded record and the user lands as admin. |

## `instance.yaml` requirements that affect auth

`config/loader.py:_validate_config` requires:

- `instance.name`
- `auth.allowed_domain` (CSV — e.g. `"groupon.com, keboola.com"`; empty allows any verified Google account)
- `auth.webapp_secret_key` (typically `"${SESSION_SECRET}"`)
- `server.host`
- `server.hostname`

If any are missing, `app/instance_config.py` catches the `ValueError`, logs `Could not load instance.yaml: ... Using defaults`, and the app keeps running with **empty** instance config. That means `get_allowed_domains()` returns `[]` and **every verified Google account is allowed**. Always grep your runtime log for `Could not load instance.yaml` after a config change — silent fallback is by design (resilience over strictness) but easy to miss.

## OAuth client setup (Google Cloud Console)

1. APIs & Services → Credentials → "Create Credentials" → "OAuth client ID" → "Web application".
2. Authorized redirect URIs — one per public hostname:
   ```
   https://<hostname>/auth/google/callback
   ```
   Add `http://localhost:8000/auth/google/callback` for local dev.
3. The Client ID and Client Secret go into `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Error 400: redirect_uri_mismatch` | Either the URI isn't registered on the OAuth client, or the app generated `http://localhost:8000/...` because `FORWARDED_ALLOW_IPS` wasn't set. | Add the URI in Console; verify `FORWARDED_ALLOW_IPS=*` reaches the container. |
| `/login?error=google_not_configured` | `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` empty in container env. | Inspect `docker compose exec app env \| grep GOOGLE`. |
| `/login?error=domain_not_allowed` | User's email domain isn't in `auth.allowed_domain`. | Add the domain (CSV) and reload — note that allowed_domain only takes effect when `instance.yaml` validates (see above). |
| Login succeeds but `/admin/*` returns "Requires role admin or higher" | New user got `role="analyst"` (default for Google-provisioned users). The JWT in the cookie is also stale. | Set `SEED_ADMIN_EMAIL` BEFORE first login, or promote in DB and have the user log out + log back in. |

## DB role promotion (when `SEED_ADMIN_EMAIL` was missed)

The system DB (`/data/state/system.duckdb`) is held exclusively by uvicorn (PID 1 in container), so `docker compose exec app python ...` can't open a second connection. Stop the app, run a throwaway container against the host volume, restart:

```bash
cd /opt/agnes
COMPOSE='docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml'
$COMPOSE stop app scheduler
docker run --rm -v /data:/data --entrypoint python ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable} -c "
import duckdb
c = duckdb.connect('/data/state/system.duckdb')
c.execute(\"UPDATE users SET role = 'admin' WHERE email = 'me@example.com'\")
c.close()
"
$COMPOSE up -d app scheduler
```

The promoted user must sign out and sign back in — JWTs carry the role at issue time and don't refresh until a new token is issued.

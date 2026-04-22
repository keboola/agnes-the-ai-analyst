# Security audit — Agnes AI Data Analyst

**Date:** 2026-04-22
**Branch audited:** `main` at commit `cbb7733`
**Method:** four parallel agents covering (1) secrets/SQLi/authz/SSRF, (2) auth flows & route wiring, (3) templates & UI wiring/XSS, (4) data layer & config & dead code. Findings deduped, severities adjusted to real-world exploitability.

Known issues already in flight are marked with their tracking links so we do not re-open them.

---

## Top 5 — fix first

### 1. `[CRITICAL]` Script-API sandbox escape → RCE for the `analyst` role

- **File:** `app/api/scripts.py:116–180`
- **Required role:** `Role.ANALYST` (not admin)
- **Trigger:** `POST /api/scripts/run` with body:
  ```python
  ().__class__.__base__.__subclasses__()[N].__init__.__globals__["system"]("id")
  ```
- **Why existing guards miss it:** the AST walker and the string allowlist block direct `import os` / `exec`, but neither stops attribute traversal through Python's class hierarchy (`__class__ → __base__ → __subclasses__() → __globals__`).
- **Impact:** arbitrary OS commands under the FastAPI process uid. Gives access to `DATA_DIR` (DuckDB files, cached parquet), `.jwt_secret`, env vars, and any credentials mounted into the container.
- **Fix (minimum):** add to the string-pattern blocklist: `__subclasses__`, `__globals__`, `__mro__`, `__bases__`, `__class__`, `__dict__`, `__code__`. In the AST walker, reject any `ast.Attribute` whose `attr` starts and ends with `__`.
- **Fix (correct):** do not run untrusted Python in-process. Either drop server-side script execution entirely, or run the sandbox in `nsjail`/gVisor/Pyodide in isolation, or gate the endpoint behind `Role.ADMIN` if it must stay.
- **Confidence:** broken (verified analytically).

### 2. `[HIGH]` `/auth/password/reset` endpoint missing — "Forgot Password?" returns 404

- **Template reference:** `app/web/templates/login_email.html:47` — `<form method="POST" action="/auth/password/reset">`
- **URL map:** `app/web/router.py:119` — `"password_auth.reset_request": "/auth/password/reset"`
- **Backend:** `app/auth/providers/password.py` only registers `/login`, `/login/web`, `/setup`. No `/reset` handler is wired.
- **Related dead code:** templates `password_reset.html` and `password_setup.html` exist but no route renders them — indicates an abandoned reset flow.
- **Tracking:** [padak/keboola_agent_cli#206](https://github.com/padak/keboola_agent_cli/issues/206)
- **Confidence:** broken.

### 3. `[HIGH]` No rate limiting on any auth endpoint

- **Files:** `app/auth/providers/password.py:36`, `app/auth/providers/email.py:53`, `app/auth/router.py:58`, `app/main.py` (middleware).
- **Evidence:** `grep -rn "slowapi\|limiter\|throttle\|ratelimit"` — zero hits in `app/`.
- **Impact:**
  - Password brute-force against `POST /auth/password/login` and `POST /auth/token`.
  - Email bombing on `POST /auth/email/send-link` — attacker floods SMTP/SendGrid quota by looping with random recipients.
  - Enumeration of bootstrap state via `POST /auth/bootstrap`.
- **Fix:** add `slowapi` with `@limiter.limit("10/minute")` on the four endpoints above, `get_remote_address` as key with proxy-aware client-IP extraction (project already has `_client_ip` helper in `app/auth/dependencies.py`).
- **Confidence:** broken.

### 4. `[HIGH]` Open-redirect bypass via backslash in `safe_next_path`

- **Files:** `app/auth/_common.py:10-24`, `app/auth/providers/password.py:95-96`, `app/web/router.py:218-219`.
- **Trigger:** `https://agnes/login?next=/\evil.com`
- **Why the current check fails:** Python sees `/\` (not `//`) and the guard `startswith("//")` does not fire. Every major browser normalizes `\` to `/` in URL paths, so `Location: /\evil.com` resolves as `//evil.com` — a cross-origin redirect.
- **Impact:** phishing — attacker crafts a link on the victim's Agnes URL, lands them on a lookalike after login.
- **Fix:**
  ```python
  if len(candidate) > 1 and candidate[1] in ("/", "\\"):
      return default
  ```
  Existing tests (`tests/test_web_ui.py:270-296`) cover `//evil.example/` but not `/\evil.com` — add the case.
- **Confidence:** broken.

### 5. `[HIGH]` SSRF in `/api/admin/configure` — `keboola_url` accepted as-is

- **File:** `app/api/admin.py:163–282`; the URL is passed straight to `KeboolaClient.test_connection()` which issues a GET request.
- **Trigger:** a compromised (or insider-threat) admin sends `{"keboola_url": "http://169.254.169.254/latest/meta-data/"}` or `http://localhost:5432/`.
- **Impact:** server as SSRF proxy to the private network — GCP/AWS instance metadata service (IAM tokens), internal databases, LAN services.
- **Why a domain allowlist is wrong:** `keboola_url` is the URL of the Keboola stack the Agnes instance connects to, **not** the Agnes host. Valid values include `connection.keboola.com`, `connection.eu-central-1.keboola.com`, `connection.europe-west3.gcp.keboola.com`, plus potentially self-hosted private Keboola stacks. A `.keboola.com` suffix check would break legitimate deployments.
- **Correct fix:** enforce `https://` scheme, then resolve the hostname and reject any result in a private / loopback / link-local / reserved block. Still allows any public HTTPS host.
  ```python
  from urllib.parse import urlparse
  import ipaddress, socket

  def _validate_stack_url(url: str) -> str | None:
      try:
          p = urlparse(url)
      except Exception:
          return "not a valid URL"
      if p.scheme != "https":
          return "must use https"
      if not p.hostname:
          return "missing hostname"
      try:
          infos = socket.getaddrinfo(p.hostname, None)
      except socket.gaierror:
          return f"cannot resolve {p.hostname}"
      for fam, _, _, _, sa in infos:
          try:
              ip = ipaddress.ip_address(sa[0])
          except ValueError:
              continue
          if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
              return f"{p.hostname} resolves to a private/loopback address ({ip})"
      return None
  ```
- **Residual risk:** DNS rebinding (hostname resolves to a public IP at validation time, then to loopback at request time). Out of scope for this fix — would need an outbound egress proxy with an IP-level ACL.
- **Confidence:** broken.

---

## Second tier (HIGH)

| # | Category | File:line | Summary | Confidence |
|---|---|---|---|---|
| 6 | race | `app/auth/providers/email.py:106-128` | `_consume_token` read-validate-clear is not atomic. Two concurrent clicks on the same magic link can both issue JWTs. Fix: `UPDATE users SET reset_token=NULL WHERE id=? AND reset_token=?` and check `rowcount == 1` before issuing the JWT. | broken |
| 7 | bootstrap | `app/auth/router.py:103-158` | Check is "any user with `password_hash`". If a seed admin exists without a password (e.g. created by `LOCAL_DEV_MODE=1` then redeployed without it, or a `SEED_ADMIN_EMAIL` without `SEED_ADMIN_PASSWORD`), `/auth/bootstrap` stays open and any caller can register a new admin account. Fix: check "any user at all" or require an explicit admin token for bootstrap. | broken |
| 8 | cookie | `app/main.py:61` | `SessionMiddleware(secret_key=...)` without `max_age` → OAuth session cookie expires only when the browser closes. Also `https_only` is not forced in production. Fix: `max_age=3600` and `https_only=bool(os.environ.get("DOMAIN"))`. | broken |
| 9 | sqli-adjacent | `connectors/keboola/extractor.py:104-106, 128` | `CREATE OR REPLACE VIEW "{table_name}" AS SELECT * FROM kbc."{bucket}"."{source_table}"` — f-string DDL with identifier quotes. Inputs come from `table_registry` (admin-controlled), but relying on quote-escaping for DDL is fragile. Fix: validate each identifier against `^[a-zA-Z0-9_]{1,63}$` before interpolation. | suspicious |
| 10 | datetime | `connectors/keboola/client.py:183` | Same offset-naive vs. offset-aware bug as the one we just fixed in `app/auth/providers/email.py`. `datetime.now() - cached_time` crashes or treats every cache entry as stale, causing needless API calls. Fix: use `datetime.now(timezone.utc)` on both sides. | broken |

---

## Third tier (MEDIUM)

| # | Category | File:line | Summary |
|---|---|---|---|
| 11 | timing | `email.py:113`, `password.py:117`, `dependencies.py:148` | Token / `token_hash` compared with `!=` instead of `hmac.compare_digest`. 32-byte entropy mitigates the brute side, but constant-time comparison is a zero-cost hardening. |
| 12 | xss-latent | `app/web/templates/_theme.html:12` | `{{ var }}: {{ val }};` inside a `<style>` block. Jinja auto-escape does not escape for CSS context. Today the values come from `instance.yaml` (FS-write), so risk is low — but it escalates to HIGH if a `/api/admin/theme` endpoint is ever added. Validate against `^[a-zA-Z0-9#()., %-]{1,50}$`. |
| 13 | config-drift | `app/instance_config.py:21-23` | `_instance_config` is cached in a module-level global with no invalidation — changes via `/api/admin/configure` or direct file edits do not take effect until the process restarts. Fix: file-mtime watch, or explicit `reload=True` kwarg on `load_instance_config`. |
| 14 | rbac-inconsistency | `app/auth/dependencies.py:203-210` | `require_admin` does an exact-match `role == "admin"` check, while `require_role(Role.ADMIN)` respects the hierarchy. Risk: if a higher role is ever added, `require_admin` rejects it. Fix: replace all call sites with `require_role(Role.ADMIN)`, delete `require_admin`. |
| 15 | sqli-defense | `src/repositories/knowledge.py:49-58` | `update(**fields)` builds `SET` clause from `fields.keys()` without an allowlist (unlike `users.py` and `metrics.py`). Today the API layer filters to a safe set of keys, but one future caller with user-controlled `**fields` turns this into SQL column injection. Add an `ALLOWED_KNOWLEDGE_FIELDS` set in the repository layer. |
| 16 | silent-fail | `services/scheduler/__main__.py:36-48` | If `SCHEDULER_API_TOKEN` is missing and auto-fetch fails, the scheduler proceeds with an empty token. API calls then 401 silently and no sync runs. Fix: `logger.error()` + non-zero exit. |
| 17 | dos-input | `src/repositories/audit.py:38-49` | `limit` is a user-supplied int passed straight into SQL `LIMIT`. `limit=1_000_000` → OOM on large audit tables. Fix: `limit = min(max(limit, 1), 1000)`. |
| 18 | pat-audit-only | `app/auth/dependencies.py:151-173` | New-IP use of a PAT logs to audit but neither blocks the request nor notifies the owner. A leaked PAT keeps working until someone reviews the audit log. Consider auto-revoke on new IP for tokens older than N days, or at least a notification hook. |
| 19 | cookie-inconsistency | `google.py:115` vs `password.py:93`, `email.py:156` | Three different rules for `secure=`: Google uses `TESTING != 1`, the others use `DOMAIN != ""`. Unify on one helper (`_is_secure_context()` keyed on `DOMAIN`). |

---

## Fourth tier (LOW) — debt, not incidents

| # | Category | File:line | Summary |
|---|---|---|---|
| 20 | path-traversal | `app/api/upload.py:77` | Target path is `md_dir / f"{user_email}.md"`. `user_email` comes from the JWT (trusted after authn) but is not sanitized — theoretical exploit requires being able to register an account with `../` in the email, which depends on the OAuth provider / domain filter. `upload_session` and `upload_artifact` correctly use `Path(raw).name`. Fix: use `user["id"]` as the filename, or `re.sub(r"[^a-zA-Z0-9@._-]", "_", email)`. |
| 21 | info-disclosure | `app/api/users.py:209` | `POST /api/users/{id}/reset-password` returns `{"reset_token": "..."}` in plaintext. Admin-only, but if the response is logged (Nginx access log, load balancer, audit proxy) the token is captured. Return `"email_sent": true` and email the token, or truncate the token in the log path. |
| 22 | dead-code | `app/web/templates/password_reset.html`, `password_setup.html`, `login_magic_link_sent.html` | Templates exist, no Python route renders them. Delete or finish the flow they belong to (password reset, magic-link "check your email" confirmation). |
| 23 | empty-schedule | `src/scheduler.py:92-99` | Empty `schedule` string silently returns "not due" — tables with misconfigured schedules never sync, no warning. Log at `WARNING` when a schedule doesn't match any known pattern. |
| 24 | silent-excepts | `src/db.py:254, 286, 309, 325, 368, 382, 489, 546`, `src/orchestrator.py:121` | `except Exception: pass`. Some are legitimate (optional `CHECKPOINT` on read-only DBs), others mask migration failures. Triage each; log at least at `DEBUG`. |

---

## Verified non-issues (do not re-open)

Several patterns looked scary at first glance but are correctly defended:

- **`UserRepository.update()` SQL injection** — one audit pass flagged this as CRITICAL, but the `allowed = {...}` allowlist at `src/repositories/users.py:50-56` drops any column not in the set before the `SET` clause is built. Safe.
- **Orchestrator `DETACH` / `ATTACH`** — identifiers are validated through `_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")` in `src/orchestrator.py` before interpolation.
- **CORS wildcard + credentials** — default `CORS_ORIGINS` is `localhost:3000,localhost:8000`, not `*`. Starlette's `CORSMiddleware` also refuses to combine `*` with `allow_credentials=True`.
- **`yaml.load` unsafe variant** — not present anywhere. Every YAML parser uses `yaml.safe_load`.
- **Hardcoded secrets in the repo** — no real secrets in `.py`/`.yaml`/`.md`. `config/deploy.yml` references env vars (`${KAMAL_REGISTRY_PASSWORD}`). Git history has no `.env` leak.
- **`LOCAL_DEV_MODE` reachable via HTTP** — the flag is read from `os.environ` only; no request header or query parameter toggles it.
- **IDOR on `/auth/tokens/{token_id}`** — handler checks `row["user_id"] != user["id"]` before returning or modifying the token. Correct.
- **Jira webhook signature** — `connectors/jira/webhook.py` uses `hmac.compare_digest`. Correct.

---

## Coverage gaps — what this audit did **not** check

| Area | Why skipped |
|---|---|
| `connectors/jira/transform.py`, `incremental_transform.py` | Scope + complex transformation logic |
| `services/corporate_memory/*`, `services/telegram_bot/*`, `services/ws_gateway/*` | Not read in depth |
| Frontend JS in `app/web/static/*.js` | Audit #3 focused on HTML templates |
| `async` / `await` correctness (blocking calls inside async handlers) | Not traced |
| Automated scanners (`bandit`, `semgrep`, `trivy`, `pip-audit`) | Not installed in the audit environment |
| Dependency CVE audit | 73 packages in `uv.lock`, not walked manually |
| `pytest` run | Test suite not executed — some findings may already be covered by existing tests |
| Docker hardening (`Dockerfile` — user namespaces, capabilities) | Out of scope |
| `infra/modules/customer-instance/` (Terraform / startup scripts) | Out of scope |

**Honest estimate:** roughly **65–70 %** of the real attack surface is covered. To close the gap I would run `bandit -r app/ src/ connectors/`, `pip-audit`, execute the `pytest` suite, and review outbound traffic logs from staging.

---

## Proposed action plan

**This week:**
- #1 script sandbox escape — one issue, RCE severity, own ticket
- #3 rate limiting on auth — `slowapi` introduction, one PR
- #4 backslash open-redirect — two-line fix plus test

**Next sprint:**
- #5 SSRF validator in `/api/admin/configure` (with the IP-range variant above, **not** a `.keboola.com` suffix)
- #6 atomic magic-link token consumption
- #7 bootstrap hardening
- #8 `SessionMiddleware` `max_age` + `https_only`

**Backlog (single tracking issue with checkboxes):**
- #11–24 — timing comparisons, theme XSS gating, config reload, RBAC unification, schedule validation, silent-except triage, dead templates cleanup.

The missing `/auth/password/reset` endpoint (#2) is already tracked in [padak/keboola_agent_cli#206](https://github.com/padak/keboola_agent_cli/issues/206).

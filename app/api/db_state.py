"""Admin endpoints for DB backend state machine.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError

from app.auth.access import require_admin
from src.db_state_machine import (
    allowed_transitions,
    read_backend_state,
)

router = APIRouter(prefix="/api/admin/db", tags=["admin-db"])


def _jobs_dir() -> Path:
    """Resolve the migration-jobs directory from DATA_DIR at call time.

    Reads ``DATA_DIR`` dynamically so tests that monkeypatch the env var
    on each fixture pick up the correct path.
    """
    return Path(os.environ.get("DATA_DIR", "/data")) / "state" / "db-jobs"


def _normalize_pg_url(url: str) -> tuple[str, int, str]:
    """Normalize a Postgres URL down to ``(host, port, database)``.

    Used to detect alias URLs that compare unequal as strings but point
    at the same physical Postgres database (B7). The comparison ignores
    user/password (credentials don't change which DB you're talking to)
    and the SQLAlchemy driver prefix (``postgresql://`` vs
    ``postgresql+psycopg://`` etc.). Host names and database names are
    lower-cased — Postgres treats them case-insensitively by convention,
    and our deployment never relies on case-distinct hosts.
    """
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    port = parsed.port or 5432
    database = (parsed.database or "").lower()
    return (host, port, database)


def _resolve_host(host: str) -> set[str]:
    """Resolve ``host`` to its IPv4/IPv6 address set. Returns empty
    set on any DNS error (caller treats empty as "unknown — fall back
    to string compare").

    B2-NEW: the pre-fix ``_urls_alias`` compared only normalised
    hostname strings, so ``postgres`` (compose service name) vs the
    sidecar container's IP (``172.18.0.2``) bypassed the guard.
    """
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
        return {info[4][0] for info in infos}
    except (socket.gaierror, OSError):
        return set()


def _urls_alias(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` point at the same physical database.

    Compares (port, db) for exact match; then either:
      - normalised hostnames match (cheap path), OR
      - the IP sets returned by ``_resolve_host`` overlap (DNS path), OR
      - one side resolved successfully and the other side's literal host
        string appears in that IP set (covers hostname-vs-IP-literal
        pairs where ``socket.getaddrinfo`` on the IP literal is mocked
        or otherwise returns empty).

    Falls back to string-equal-only when DNS fails for both sides.
    B2-NEW: pre-fix compared only normalised hostname strings — inside
    the migrator container, ``postgres`` (compose service name) vs
    ``172.18.0.2`` (sidecar IP) bypassed the alias guard.
    """
    a_host, a_port, a_db = _normalize_pg_url(a)
    b_host, b_port, b_db = _normalize_pg_url(b)
    if (a_port, a_db) != (b_port, b_db):
        return False
    if a_host == b_host:
        return True
    a_ips = _resolve_host(a_host)
    b_ips = _resolve_host(b_host)
    if a_ips and b_ips:
        return bool(a_ips & b_ips)
    # One side resolved; check if the other side's literal host string
    # appears in the resolved IP set.  This catches hostname-vs-IP-literal
    # pairs (e.g. ``postgres`` → {172.18.0.2} vs host ``172.18.0.2``).
    if a_ips and b_host in a_ips:
        return True
    if b_ips and a_host in b_ips:
        return True
    # Both unresolvable → conservative non-alias.
    return False


def _classify_reserved_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> "str | None":
    """Return a short human-readable category if ``ip`` falls in a
    reserved range that the API must refuse to dial, else None.

    Extracted from the pre-fix inline ladder in ``_validate_cloud_url``
    so Task 2 (B1-NEW pinned-IP propagation) can reuse the same
    classification. MED-1-PARTIAL extends usage from IP-literal-only
    to DNS-resolved hosts.
    """
    import ipaddress

    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (covers GCE/AWS metadata service)"
    if ip.is_private:
        return "private (RFC1918 / IPv6 ULA)"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "CGNAT (RFC6598)"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved/multicast/unspecified"
    return None


def _pin_resolved_ip(url: str) -> str:
    """Return ``url`` with the hostname replaced by its resolved IP literal.

    If the host is already an IP literal, the original URL is returned
    unchanged — the address IS already pinned, no resolution needed.
    If resolution fails, the original URL is returned as a safe fallback
    (the migrator's TCP connect will fail downstream, not silently succeed
    against the wrong host).

    B1-NEW: the pre-fix code persisted the hostname-bearing URL verbatim
    into the pending job JSON; the applier then passed it unresolved to
    psycopg. A DNS rebind between API validation and migrator connect let
    an attacker-controlled hostname point at the local sidecar AFTER the
    alias guard passed — self-migration committed, the next cloud-only
    applier tick stopped the only live Postgres.

    Pinning at queue time closes the window: the migrator dials the exact
    IP that was validated, not a re-resolved one.

    The chosen IP when ``getaddrinfo`` returns multiple addresses is
    ``sorted()[0]`` (lexicographic ascending) — deterministic across
    runs, preserving replay reproducibility.
    """
    from urllib.parse import urlparse, urlunparse
    import ipaddress as _ip

    try:
        parsed = urlparse(url)
    except Exception:
        return url  # malformed — return as-is; caller already validated
    host = parsed.hostname
    if not host:
        return url
    # If host is already a literal IP, it IS the pinned form.
    try:
        _ip.ip_address(host)
        return url  # already pinned
    except ValueError:
        pass
    resolved = sorted(_resolve_host(host))
    if not resolved:
        return url  # DNS failure — fall back to hostname URL
    chosen = resolved[0]
    # Rebuild netloc: preserve userinfo and port, substitute host.
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = f"{userinfo}{chosen}{port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _validate_cloud_url(url: str) -> None:
    """Reject obviously-wrong cloud URLs early (H3) and reserved/private
    addresses (MED-2, MED-1-PARTIAL).

    H3 — Required: scheme starts with ``postgresql``, host non-empty,
    database non-empty. Catches misclicks (sqlite://, file://, http://)
    and incomplete URLs (missing host or DB name) before any
    state-machine writes happen.

    MED-2 — Reject loopback / GCE metadata (169.254.169.254) /
    RFC1918 private / link-local / CGNAT (RFC6598) / IPv6 ULA. Without
    this, an admin posting ``cloud_url=postgresql://x:y@169.254.169.254:5432/db``
    triggers ``alembic upgrade head`` opening a TCP socket to the GCE
    metadata server — the server-fingerprint error in the job's
    ``error.message`` then leaks service liveness. SSRF / port-probe
    primitive from any admin path.

    MED-1-PARTIAL — The round-2 fix (commit 46334442) rejected IP
    literals but ``except ValueError: return`` short-circuited
    validation for any non-literal hostname. An attacker-controlled DNS
    entry pointing ``metadata.google.internal`` (or similar) at
    ``169.254.169.254`` then bypassed the guard. ``_resolve_host`` now
    feeds ``socket.getaddrinfo`` results through the same classification
    ladder via ``_classify_reserved_ip``; if *any* resolved IP is
    reserved the URL is refused.

    Opt-in test override: ``AGNES_ALLOW_RESERVED_CLOUD_URL=1`` skips
    the reserved-range check (used by the test harness for fixtures
    pointing at 127.0.0.1).
    """
    import ipaddress

    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError) as e:
        raise HTTPException(400, detail=f"cloud_url is not a valid URL: {e}")
    scheme = parsed.drivername or ""
    if not scheme.startswith("postgresql"):
        raise HTTPException(
            400,
            detail=f"cloud_url scheme must start with 'postgresql', got '{scheme}'",
        )
    if not parsed.host:
        raise HTTPException(400, detail="cloud_url must include a host")
    if not parsed.database:
        raise HTTPException(400, detail="cloud_url must include a database name")

    # === MED-2 + MED-1-PARTIAL: reserved-range rejection ===
    if os.environ.get("AGNES_ALLOW_RESERVED_CLOUD_URL") == "1":
        return  # explicit test/dev opt-in

    host = parsed.host
    # ``localhost`` — reject without DNS resolution.
    if host.lower() == "localhost":
        raise HTTPException(
            400,
            detail=(
                "cloud_url host is loopback (localhost) — reserved; "
                "set AGNES_ALLOW_RESERVED_CLOUD_URL=1 for tests/dev"
            ),
        )

    # Build the IP set to classify: either the single literal, or every
    # address ``host`` resolves to.  MED-1-PARTIAL — pre-fix the function
    # returned early on a non-literal hostname, so an attacker-controlled
    # DNS entry pointing at e.g. 169.254.169.254 bypassed the guard.
    try:
        literal = ipaddress.ip_address(host)
        ips_to_check = [literal]
    except ValueError:
        resolved = _resolve_host(host)
        if not resolved:
            # DNS failure: conservative allow — the migrator's TCP
            # connect attempt will fail cleanly downstream, and we
            # don't want to break operators behind broken or slow DNS.
            return
        ips_to_check = []
        for raw in resolved:
            try:
                ips_to_check.append(ipaddress.ip_address(raw))
            except ValueError:
                continue

    for ip in ips_to_check:
        category = _classify_reserved_ip(ip)
        if category is not None:
            raise HTTPException(
                400,
                detail=(
                    f"cloud_url host {host!r} resolves to {ip} which is "
                    f"{category}; reserved"
                ),
            )


def _redact_url(url: str | None) -> str | None:
    """Return ``url`` with every password placement masked.

    Returns ``None`` for falsy input (``None`` or empty string).

    Round-2 review MED-3 — the previous regex
    ``(://[^:]+:)[^@]+(@)`` only matched ``://user:pass@host`` userinfo
    style and let ``?password=secret`` query-string style leak. Route
    through ``sqlalchemy.engine.make_url`` which understands every
    libpq form (userinfo, query string, URL-encoded chars).
    """
    if not url:
        return None
    try:
        redacted = make_url(url).render_as_string(hide_password=True)
        # SQLAlchemy masks userinfo passwords but leaves ?password=<value>
        # and ?sslpassword=<value> in the query string verbatim.  Strip
        # both explicitly so every libpq URL-embedded credential parameter
        # is safe.  ``passfile=`` is a path to a credential file, not a
        # credential itself — the anchored alternation leaves it alone.
        redacted = re.sub(r"(?i)([?&](?:password|sslpassword)=)[^&]*", r"\1***", redacted)
        return redacted
    except Exception:
        # Unparseable — never echo the input back (could still carry
        # creds if it happened to be a valid-looking URL with a typo).
        return "<unparseable-url>"


def _redact_urls_in_text(text: str | None) -> str | None:
    """Mask every URL-shaped substring in arbitrary text via
    :func:`_redact_url`. Used to scrub ``error.message`` /
    ``error.detail`` fields where a raised exception captured the
    target URL verbatim. H3-NEW.
    """
    if not text:
        return text
    # Liberal URL match — anything that looks like ``scheme://...``
    # bounded by whitespace, quotes, parens, or end-of-string.
    pattern = re.compile(r"""[a-z][a-z0-9+.\-]*://[^\s'"()<>]+""", re.IGNORECASE)
    return pattern.sub(lambda m: _redact_url(m.group(0)) or "<redacted>", text)


def _redact_error_payload(err: dict | None) -> dict | None:
    """Recursively redact URL-shaped substrings inside an ``error``
    dict before serialisation. H3-NEW.
    """
    if not err or not isinstance(err, dict):
        return err
    out: dict = {}
    for k, v in err.items():
        if isinstance(v, str):
            out[k] = _redact_urls_in_text(v)
        elif isinstance(v, dict):
            out[k] = _redact_error_payload(v)
        else:
            out[k] = v
    return out


def _applier_last_tick_age_s() -> int | None:
    """Seconds since the host applier touched its heartbeat file, or
    ``None`` if the file is missing.

    The applier touches ``<DATA_DIR>/state/agnes-state-applier.tick``
    at the start of every invocation (Phase 4). UI uses this to warn
    when the systemd timer is broken or the unit is disabled — without
    it, pending migration jobs queue silently because nothing is
    advancing them. None signals "applier has never run" (fresh install
    or broken unit).
    """
    import time
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    tick = data_dir / "state" / "agnes-state-applier.tick"
    if not tick.exists():
        return None
    try:
        # max(0, ...) defends against clock skew where the file mtime
        # is briefly in the future (NTP slew, VM snapshot restore) —
        # a negative age would confuse the UI's threshold logic.
        return max(0, int(time.time() - tick.stat().st_mtime))
    except OSError:
        return None


def _current_job_id() -> str | None:
    """Return ``job_id`` of any currently-active migration job, else None.

    "Active" = ``pending`` (queued, awaiting applier pickup) OR ``running``
    (applier is driving the migrator subprocess). The pending state was
    previously omitted from this check (B8), which made GET /state report no
    current job during the ~30s applier-pickup window — the UI showed "no
    migration in progress" while the state machine already sat at
    ``*_in_progress``.

    Running takes priority over pending: when both files exist (theoretically
    impossible under normal lock discipline, but defensive), the UI should
    surface the actively-executing work.
    """
    jobs_dir = _jobs_dir()
    if not jobs_dir.exists():
        return None
    pending: str | None = None
    for path in jobs_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        status = data.get("status")
        if status == "running":
            return data.get("job_id")
        if status == "pending" and pending is None:
            pending = data.get("job_id")
    return pending


@router.get("/state", dependencies=[Depends(require_admin)])
def get_db_state() -> dict:
    """Current backend + allowed transitions + in-progress job (if any)
    + host applier liveness (Phase 4)."""
    state, url = read_backend_state()
    return {
        "backend": state.value,
        "url_redacted": _redact_url(url),
        "allowed_transitions": [t.value for t in allowed_transitions(state)],
        "current_job_id": _current_job_id(),
        "applier_last_tick_age_s": _applier_last_tick_age_s(),
    }


class MigrateRequest(BaseModel):
    """Body for ``POST /api/admin/db/migrate``."""
    target: str  # "side_car" or "cloud"
    cloud_url: str | None = None  # required when target=cloud


@router.post("/migrate", status_code=202, dependencies=[Depends(require_admin)])
def start_migration(payload: MigrateRequest) -> dict:
    """Queue a backend migration job for the host applier daemon.

    The endpoint does NOT execute the migration — it only writes the
    intent. The ``agnes-state-applier`` host daemon picks up the
    pending job within ~30s, stops the app container (releasing the
    DuckDB file lock — see the docstring at the top of
    ``scripts/ops/agnes-state-applier.sh`` for why in-process release
    isn't viable), runs the migrator subprocess on the host, then
    restarts the app on the new backend.

    Effects of this call:
      1. Validates the transition against the current state.
      2. Acquires the non-blocking migration flock (409 if held).
      3. Writes ``/data/state/instance.yaml::backend = *_in_progress``.
      4. Writes ``/data/state/db-jobs/<job_id>.json`` with
         ``status="pending"`` plus the target URL + backend so the
         applier has everything it needs to invoke the migrator.
      5. Writes ``/data/state/db-state-target.flag`` — the lifecycle
         signal the applier polls on.

    Returns 202 with ``{job_id, status: "pending"}``. Clients poll
    ``GET /api/admin/db/job/{id}`` for progress; the applier overwrites
    the same file with running → success / failed.
    """
    import json as _json
    from src.db_state_machine import (
        BackendState,
        BackendNotYetSupportedError,
        InvalidTransitionError,
        MigrationInProgressError,
        MigrationLock,
        validate_transition,
        write_backend_state,
    )

    # ── Block 1: cheap pre-lock rejections ────────────────────────────────
    # These checks are stateless (no state-machine reads) and cheap to
    # evaluate without holding the flock.  Reject early to avoid
    # unnecessary lock contention.

    # Validate the target token is a known BackendState value.
    try:
        target_state = BackendState(payload.target)
    except ValueError:
        raise HTTPException(400, detail=f"Unknown target: {payload.target}")

    # Pre-lock fast-path: validate the transition against the current state
    # so that structurally invalid requests (self-loops, disallowed moves)
    # are rejected immediately without acquiring the flock.  The
    # authoritative check is re-done inside the lock (B1-NEW) — this read
    # is best-effort early rejection only.
    #
    # Skip when pre_lock_state is already *_in_progress: a concurrent
    # caller got there first and the in-lock _current_job_id() check will
    # return 409.  Validating here would produce a misleading 400
    # ("cloud_in_progress → side_car not allowed") instead.
    pre_lock_state, _ = read_backend_state()
    if not pre_lock_state.value.endswith("_in_progress"):
        try:
            validate_transition(pre_lock_state, target_state)
        except InvalidTransitionError as e:
            raise HTTPException(400, detail=str(e))
        except BackendNotYetSupportedError as e:
            raise HTTPException(501, detail=str(e))

    # H7-NEW: reverse migrations to DuckDB are reserved in
    # _ALLOWED_TRANSITIONS but the migrator does not yet wire
    # ``target='duckdb'`` / ``'duckdb_quack'``. Reject at the endpoint
    # with 501 so the API contract is honest — versus silently
    # mis-routing to CLOUD because the branch logic was
    # ``payload.target == 'side_car' else cloud``.
    # Placed AFTER validate_transition so a duckdb → duckdb self-loop
    # returns 400 (invalid transition) rather than 501 (not yet supported).
    if payload.target in ("duckdb", "duckdb_quack"):
        raise HTTPException(
            status_code=501,
            detail=(
                f"target={payload.target!r} is reserved in the state-machine "
                "matrix but the migrator does not yet support reverse "
                "migrations to DuckDB. Not yet supported — tracked for a "
                "follow-up release."
            ),
        )

    # MED-2 + scheme validation: URL structural checks are stateless and
    # cheap; do them before locking.
    if payload.target == "cloud":
        if not payload.cloud_url:
            raise HTTPException(400, detail="cloud_url required for target=cloud")
        _validate_cloud_url(payload.cloud_url)

    # ── Block 2: acquire flock, re-read state, validate, write ───────────
    # B1-NEW: the pre-fix ordering was validate → flock → write.  Two
    # admins racing through validate_transition before either took the
    # lock both passed, then both wrote pending jobs (the second
    # clobbered the first's flag file).  Fix: move ALL state-reading and
    # validation INSIDE the flock so the second caller re-reads state
    # under the lock and sees the first caller's pending job → 409.
    try:
        lock = MigrationLock()
        lock.__enter__()
    except MigrationInProgressError:
        existing = _current_job_id()
        raise HTTPException(
            409,
            detail=f"Migration already in progress: job {existing}",
        )

    try:
        # Re-read current state under the lock so we see any writes made
        # by a concurrent caller that already acquired the lock.
        current_state, current_url = read_backend_state()

        # Surface pending jobs (B8) — checked here, under the lock, so a
        # just-written pending job is visible to the second caller.
        existing_job = _current_job_id()
        if existing_job:
            raise HTTPException(
                409,
                detail=f"Migration already in progress: job {existing_job}",
            )

        # Validate the transition matrix under the lock so both callers
        # cannot both pass validation against a stale current_state read.
        try:
            validate_transition(current_state, target_state)
        except InvalidTransitionError as e:
            raise HTTPException(400, detail=str(e))
        except BackendNotYetSupportedError as e:
            raise HTTPException(501, detail=str(e))

        # Resolve target URL (needs current_state for source_url).
        if payload.target == "side_car":
            password = os.environ.get("POSTGRES_PASSWORD")
            if not password:
                raise HTTPException(
                    500,
                    detail=(
                        "POSTGRES_PASSWORD env var missing on server — set it via "
                        "Terraform / .env / docker compose override before migrating "
                        "to side_car. The migration cannot proceed with an unknown "
                        "credential."
                    ),
                )
            target_url = f"postgresql+psycopg://agnes:{password}@postgres:5432/agnes"
        else:
            target_url = payload.cloud_url

        # Source URL — only present when source is a PG backend. The
        # applier passes it to the migrator's --source-url.
        source_url = current_url if current_state in (
            BackendState.SIDE_CAR, BackendState.CLOUD
        ) else None

        # H1-NEW: refuse to queue a PG-source migration when
        # instance.yaml has no URL recorded for the current backend.
        # Pre-fix the migrator would crash later with "--source-url is
        # required" and the rollback path would write empty url —
        # leaving backend=<source> + no url, requiring manual YAML
        # repair to recover.
        if current_state in (BackendState.SIDE_CAR, BackendState.CLOUD) and not source_url:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cannot migrate FROM {current_state.value}: source_url is None "
                    f"because instance.yaml's database.url is missing — manually "
                    f"set database.url in /data/state/instance.yaml first, "
                    f"then retry"
                ),
            )

        # Reject same-DB cycles — would silently put two readers on the
        # same physical Postgres after the cutover, which is data-loss
        # destructive once the source side is wiped. The alias check
        # normalizes credentials, default port, and driver prefix so that
        # cosmetic URL differences cannot bypass the guard (B7).
        # Also under the lock (B1-NEW) so two callers with alias URLs
        # cannot both pass if the source state changed between them.
        if source_url and _urls_alias(source_url, target_url):
            raise HTTPException(
                400,
                detail="source and target URL alias the same Postgres database — refusing to migrate onto self",
            )

        job_id = str(uuid.uuid4())

        in_progress = (
            BackendState.SIDE_CAR_IN_PROGRESS if payload.target == "side_car"
            else BackendState.CLOUD_IN_PROGRESS
        )
        write_backend_state(in_progress)

        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        jobs_dir = data_dir / "state" / "db-jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Pending job payload — the applier reads target_url +
        # source_backend + target_backend to compose the migrator
        # invocation, and overwrites this file with running/success/
        # failed as the migrator progresses.
        #
        # ``queued_at`` is a UTC ISO timestamp used by the applier
        # to expire stale pending jobs (H8 — operator masks the
        # applier timer, queues a migration, fixes state manually,
        # then unmasks days later; we don't want the applier to
        # blindly run an old intent against now-incompatible state).
        # B1-NEW: resolve hostnames to IP literals at queue time so the
        # applier passes a pinned address to the migrator.  This closes the
        # DNS rebinding window — the migrator dials the IP that passed
        # validation, not a potentially re-resolved one.  The display URL
        # (target_url / source_url) keeps the hostname so operators see what
        # they posted; the applier prefers the *_pinned_ip variant.
        # schema_version bumped 1 → 2 for the two new optional fields.
        # Readers must treat them as optional for backwards compat with v1
        # jobs queued before this fix was deployed.
        target_url_pinned_ip = _pin_resolved_ip(target_url) if target_url else None
        source_url_pinned_ip = _pin_resolved_ip(source_url) if source_url else None

        intent = {
            "job_id": job_id,
            "schema_version": 2,
            "status": "pending",
            "source_backend": current_state.value,
            "target_backend": payload.target,
            "target_url": target_url,
            "target_url_pinned_ip": target_url_pinned_ip,
            "source_url": source_url,
            "source_url_pinned_ip": source_url_pinned_ip,
            "progress_pct": 0,
            "current_step": "queued",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        job_path = jobs_dir / f"{job_id}.json"
        tmp_path = job_path.with_suffix(".json.tmp")
        tmp_path.write_text(_json.dumps(intent, indent=2))
        os.replace(tmp_path, job_path)
        os.chmod(job_path, 0o600)

        # Flag tells the applier WHICH compose lifecycle to settle on.
        flag_target = (
            "side-car-enabled" if payload.target == "side_car"
            else "cloud-only"
        )
        flag_path = data_dir / "state" / "db-state-target.flag"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_tmp = flag_path.with_suffix(".flag.tmp")
        flag_tmp.write_text(flag_target)
        os.replace(flag_tmp, flag_path)
    finally:
        lock.__exit__(None, None, None)

    # ── Block 3: response ─────────────────────────────────────────────────
    return {"job_id": job_id, "status": "pending"}


@router.get("/job/{job_id}", dependencies=[Depends(require_admin)])
def get_job(job_id: str) -> dict:
    """Return migration job status (poll target for POST /migrate clients).

    URLs in the response body are redacted (passwords replaced with
    ``****``). Admins viewing job progress should never see the live
    DB credentials — H1. The raw file on disk keeps the unredacted
    URL because the applier subprocess needs it to invoke the migrator.
    """
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")
    data = json.loads(path.read_text())
    if "target_url" in data:
        data["target_url"] = _redact_url(data["target_url"])
    if "source_url" in data:
        data["source_url"] = _redact_url(data["source_url"])
    # H3-NEW: scrub URL-shaped substrings from the entire error payload
    # before serialisation. The alembic-timeout RuntimeError embeds the
    # raw target URL via !r into error.message; _redact_url only masks
    # top-level fields so without this pass plaintext creds leak into
    # HTTP responses, browser history, and UI screenshots.
    if "error" in data:
        data["error"] = _redact_error_payload(data["error"])
    return data


@router.post("/cancel/{job_id}", dependencies=[Depends(require_admin)])
def cancel_job(job_id: str) -> dict:
    """Cancel a running migration before point-of-no-return."""
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")

    data = json.loads(path.read_text())

    # H1-NEW: any terminal state means the flip already committed (completed),
    # the migration already failed, or the job was already cancelled. A
    # "cancel after the fact" must NOT rewrite instance.yaml — return 409 so
    # the operator knows the action was a no-op and can inspect the real state.
    if data["status"] in ("completed", "failed", "cancelled"):
        raise HTTPException(
            409,
            detail=(
                f"job {job_id} is already in terminal state ({data['status']}); "
                "cancel is a no-op"
            ),
        )

    if data["status"] != "running":
        raise HTTPException(
            400, detail=f"Job is {data['status']}; cannot cancel non-running job"
        )
    if data["current_step"] in ("flip_backend", "app_restart", "verify_health"):
        raise HTTPException(
            409,
            detail="Past point-of-no-return (step >= flip_backend); manual recovery required"
        )

    # H1-NEW ordering: write the cancel sentinel FIRST so the migrator's final
    # pre-flip re-check (_check_cancel_before_flip) can observe it. Pre-fix the
    # JSON status was updated before the sentinel — the migrator could pass the
    # sentinel check and proceed to flip while the API was still writing the
    # revert. Now: sentinel is present before any state-machine revert, making
    # cancel ↔ flip mutually exclusive.
    #
    # Signal the migrator subprocess (B2). The sentinel file is a
    # cooperative cancellation marker — the migrator polls for it at
    # step boundaries and raises JobCancelled when it observes the file.
    sentinel = _jobs_dir() / f"{job_id}.cancel"

    # H1-PARTIAL: hold MigrationLock across the cancel sentinel write
    # AND the instance.yaml revert. Pre-fix the two operations were
    # separate, leaving a microsecond window where the migrator's
    # _check_cancel_before_flip could pass and then write_backend_state
    # could overwrite the cancel's revert — split-brain end state with
    # data on TARGET but instance.yaml on SOURCE. The migrator side now
    # also acquires the lock around its check+flip; lock acquisition
    # here makes the two writes mutually exclusive at the OS level.
    from src.db_state_machine import (
        BackendState,
        MigrationInProgressError,
        MigrationLock,
        write_backend_state,
    )

    def _do_cancel_under_lock() -> None:
        # H1-PARTIAL atomic re-check: under the lock, re-read the job
        # JSON so that if the migrator completed its flip (or moved
        # past PNR) BETWEEN the pre-lock check and our lock acquisition,
        # we bail without writing the sentinel or reverting
        # instance.yaml — that would produce split-brain (data on
        # TARGET + instance.yaml reverted to SOURCE).
        fresh = json.loads(path.read_text())
        if fresh["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(
                409,
                detail=(
                    f"job {job_id} reached terminal state ({fresh['status']}) "
                    "while cancel was acquiring the migration lock; cancel is a no-op"
                ),
            )
        if fresh["current_step"] in ("flip_backend", "app_restart", "verify_health"):
            raise HTTPException(
                409,
                detail=(
                    "Job moved past point-of-no-return (step >= flip_backend) "
                    "while cancel was acquiring the migration lock; manual recovery required"
                ),
            )
        sentinel.touch()
        fresh["status"] = "cancelled"
        fresh["completed_at"] = datetime.now(timezone.utc).isoformat()
        fresh["error"] = {
            "step": fresh["current_step"],
            "class": "Cancelled",
            "message": "Admin cancelled migration",
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fresh, indent=2, sort_keys=True))
        os.replace(tmp, path)
        os.chmod(path, 0o600)

        # Revert state machine to the source backend captured when the
        # migration kicked off.  The URL was preserved across the *_in_progress
        # write (B4), so a no-url write here keeps the live source URL.
        #
        # MED-4: when reverting cancel to duckdb, the target's postgres URL
        # must NOT survive in the overlay. write_backend_state with url=None
        # drops the key (vs the Ellipsis sentinel which would PRESERVE the
        # current key — that's the B4 fix in round 1).
        source_backend = fresh["source_backend"]
        revert_url = None if source_backend == "duckdb" else ...
        write_backend_state(BackendState(source_backend), url=revert_url)

    try:
        with MigrationLock():
            _do_cancel_under_lock()
    except MigrationInProgressError:
        # The migrator subprocess is currently holding the lock to do
        # its own flip. It will check our sentinel right before the
        # flip; if it's already past the check, the flip will commit
        # and the next cancel call will see status=completed → 409.
        # Briefly retry — the lock is held for at most one flip's
        # worth of file I/O.
        time.sleep(0.5)
        with MigrationLock():
            _do_cancel_under_lock()

    return {"cancelled": True}

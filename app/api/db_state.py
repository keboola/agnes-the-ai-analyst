"""Admin endpoints for DB backend state machine.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import re
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


def _urls_alias(a: str, b: str) -> bool:
    """True iff two Postgres URLs point at the same physical database.

    See :func:`_normalize_pg_url` for the normalization rules. Used by
    the migrate endpoint to reject "migrate onto self" attempts (B7).
    """
    return _normalize_pg_url(a) == _normalize_pg_url(b)


def _validate_cloud_url(url: str) -> None:
    """Reject obviously-wrong cloud URLs early (H3) and reserved/private
    addresses (MED-2).

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

    # === MED-2: reserved-range rejection ===
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
    # IP literal?
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname → DNS resolution happens later in psycopg; pre-class can't classify
    if ip.is_loopback:
        raise HTTPException(
            400,
            detail=f"cloud_url host is loopback ({ip}); reserved",
        )
    if ip.is_link_local:
        # Includes 169.254.169.254 (GCE/AWS IMDS).
        raise HTTPException(
            400,
            detail=(
                f"cloud_url host is link-local ({ip}) — covers GCE/AWS metadata service; reserved"
            ),
        )
    if ip.is_private:
        raise HTTPException(
            400,
            detail=f"cloud_url host is private ({ip}); reserved (RFC1918 / IPv6 ULA)",
        )
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        raise HTTPException(
            400,
            detail=f"cloud_url host is CGNAT ({ip}); reserved (RFC6598)",
        )
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        raise HTTPException(
            400,
            detail=f"cloud_url host is reserved/multicast/unspecified ({ip})",
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

    current_state, current_url = read_backend_state()
    try:
        target_state = BackendState(payload.target)
    except ValueError:
        raise HTTPException(400, detail=f"Unknown target: {payload.target}")

    try:
        validate_transition(current_state, target_state)
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

    if payload.target == "cloud":
        if not payload.cloud_url:
            raise HTTPException(400, detail="cloud_url required for target=cloud")
        _validate_cloud_url(payload.cloud_url)

    # Resolve target URL.
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

    # Reject same-DB cycles — would silently put two readers on the
    # same physical Postgres after the cutover, which is data-loss
    # destructive once the source side is wiped. The alias check
    # normalizes credentials, default port, and driver prefix so that
    # cosmetic URL differences cannot bypass the guard (B7).
    if source_url and _urls_alias(source_url, target_url):
        raise HTTPException(
            400,
            detail="source and target URL alias the same Postgres database — refusing to migrate onto self",
        )

    job_id = str(uuid.uuid4())

    # Acquire lock — non-blocking; 409 if a peer already holds it.
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
        intent = {
            "job_id": job_id,
            "schema_version": 1,
            "status": "pending",
            "source_backend": current_state.value,
            "target_backend": payload.target,
            "target_url": target_url,
            "source_url": source_url,
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
    return data


@router.post("/cancel/{job_id}", dependencies=[Depends(require_admin)])
def cancel_job(job_id: str) -> dict:
    """Cancel a running migration before point-of-no-return."""
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")

    data = json.loads(path.read_text())
    if data["status"] != "running":
        raise HTTPException(
            400, detail=f"Job is {data['status']}; cannot cancel non-running job"
        )
    if data["current_step"] in ("flip_backend", "app_restart", "verify_health"):
        raise HTTPException(
            409,
            detail="Past point-of-no-return (step >= flip_backend); manual recovery required"
        )

    # Signal the migrator subprocess (B2). The sentinel file is a
    # cooperative cancellation marker — the migrator polls for it at
    # step boundaries and raises JobCancelled when it observes the
    # file. We write the sentinel BEFORE flipping the job JSON status
    # so a slow migrator that polls slightly later still sees the
    # signal.
    sentinel = _jobs_dir() / f"{job_id}.cancel"
    sentinel.touch()

    from datetime import datetime, timezone
    data["status"] = "cancelled"
    data["completed_at"] = datetime.now(timezone.utc).isoformat()
    data["error"] = {
        "step": data["current_step"],
        "class": "Cancelled",
        "message": "Admin cancelled migration",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
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
    from src.db_state_machine import BackendState, write_backend_state
    source_backend = data["source_backend"]
    revert_url = None if source_backend == "duckdb" else ...
    write_backend_state(BackendState(source_backend), url=revert_url)

    return {"cancelled": True}

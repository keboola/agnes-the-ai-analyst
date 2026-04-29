"""Admin endpoints — table discovery, registry management, instance configuration.

Every gate on this router uses ``require_admin`` from ``app.auth.access``,
which checks Admin user_group membership for both OAuth session and PAT
callers via the same ``_user_group_ids`` lookup.
"""

import logging
import os
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.audit import AuditRepository
from src.identifier_validation import (
    is_safe_identifier as _is_safe_identifier,
    is_safe_quoted_identifier as _is_safe_quoted_identifier,
)
from src.sql_safe import is_safe_project_id as _is_safe_project_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# Serializes the read-modify-write of state/instance.yaml across the two
# endpoints that mutate the overlay (POST /server-config and POST /configure).
# Without it, two admins saving concurrently would each read the same overlay
# snapshot, merge their disjoint patches, and the second os.replace would silently
# drop the first patch. Single-process FastAPI workers; multi-worker deployments
# would need an OS-level file lock — documented limitation.
_overlay_write_lock = threading.Lock()

# SSRF protection: reject private/internal URLs for keboola_url
import ipaddress as _ipaddress
import socket as _socket
from urllib.parse import urlparse as _urlparse


def _validate_url_not_private(url: str, field_name: str = "url") -> None:
    """Raise 400 if the URL host points to a private/reserved network.

    Uses DNS resolution + ipaddress checks instead of hostname regex,
    which correctly handles all IPv4/IPv6 addresses including abbreviated
    forms (fe80::1, ::1, etc.) and DNS rebinding (resolves at check time).
    """
    try:
        parsed = _urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: not a valid URL")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: missing hostname")

    # Reject well-known dangerous hostnames before DNS resolution
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: must not point to a private or reserved network",
        )

    # Resolve hostname to IP addresses and check each one
    try:
        addrinfos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: could not resolve hostname",
        )

    for family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip = _ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field_name}: must not point to a private or reserved network",
            )


def _normalize_primary_key(v):
    """Coerce a string primary_key to ``[v]`` for backward compatibility.

    The 0.14.0 contract is ``Optional[List[str]]`` so composite primary keys
    (e.g. session-grain tables keyed on ``(session_id, event_date)``) round-
    trip cleanly. Pre-0.14.0 callers sent a single string; Pydantic v2
    refuses to coerce, so without this validator a CLI script posting
    ``"primary_key": "session_id"`` would now hit a 422. Wrap a bare string
    in a one-element list so old and new callers both work.
    """
    if v is None:
        return v
    if isinstance(v, str):
        return [v]
    return v


# Patches to these section paths must pass _validate_url_not_private. The
# tuple is `(section, *intermediate_keys, leaf_key)` — same SSRF gate the
# /configure wizard applies to keboola_url, so an admin can't sneak
# http://169.254.169.254/ in via the server-config editor's data_source patch.
_URL_BEARING_FIELDS: tuple[tuple[str, ...], ...] = (
    ("data_source", "keboola", "stack_url"),
)


def _validate_urls_in_patch(sections: Dict[str, Dict[str, Any]]) -> None:
    """Apply SSRF protection to every URL-bearing field present in the patch.

    Walks each registered ``(section, *path, leaf)`` against the incoming
    patch and runs ``_validate_url_not_private`` on any string value found.
    Missing intermediate keys / non-dict nodes are silently skipped — the
    patch hasn't touched that field, no validation needed.
    """
    for path in _URL_BEARING_FIELDS:
        section = path[0]
        if section not in sections:
            continue
        node: Any = sections[section]
        for key in path[1:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, dict):
            value = node.get(path[-1])
            if isinstance(value, str) and value:
                _validate_url_not_private(value, field_name=".".join(path))


# --- Server-config (instance.yaml) editor -----------------------------------
#
# The /admin/server-config UI POSTs a partial dict here keyed by section
# (instance, data_source, email, telegram, jira, theme, server, auth) with
# the field values to merge into instance.yaml. Each save:
#   1. Loads the current instance.yaml (writable overlay first, then static).
#   2. Deep-merges the patch on top.
#   3. Writes to DATA_DIR/state/instance.yaml (the writable overlay).
#   4. Writes one audit_log entry tagged `instance_config.update` containing
#      a sanitized diff (secret-looking keys are masked).
# Hot-reload is OUT OF SCOPE for #91 — the response carries
# `restart_required=True` so the UI can show the banner.

# Sections an admin can mutate. Keep the list explicit so a typo'd section
# in the request body is rejected loudly instead of being silently merged
# into the YAML root and confusing future loads.
_EDITABLE_SECTIONS: tuple[str, ...] = (
    "instance",
    "data_source",
    "email",
    "telegram",
    "jira",
    "theme",
    "server",
    "auth",
)

# "Danger-zone" sections — flipping these can lock operators out (auth.*) or
# break OAuth callbacks (server.hostname/host). The UI shows a confirmation
# dialog before submitting them. The API accepts them; this list exists so
# the audit entry can flag the change as high-risk and the UI can surface
# the right warning copy.
_DANGER_SECTIONS: tuple[str, ...] = ("auth", "server")

# Keys whose values must be redacted from the audit diff. We match
# substring (case-insensitive) so `client_secret`, `api_token`,
# `webapp_secret_key`, `bot_token`, `password`, `smtp_password`, etc. all
# get masked even when nested.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "api_key",
)


def _is_secret_key(key: str) -> bool:
    """True if a config key holds a credential and should be masked in audit logs."""
    k = key.lower()
    return any(pat in k for pat in _SECRET_KEY_PATTERNS)


def _mask(value: Any) -> str:
    """Replacement value used in the audit diff for secret fields.

    We deliberately do NOT preserve length or any hint about the secret —
    the diff is read by other admins, and there's no operator value to
    leaking "the new SMTP password is 16 chars". `***` is enough to show
    that the field changed without exposing it.
    """
    if value in (None, ""):
        return "<empty>"
    return "***"


# Sentinel values produced by `_mask`. Any patch leaf that arrives at a
# secret-keyed slot still bearing one of these strings means the caller
# round-tripped the GET payload (which redacts secret-keyed children inside
# nested objects) without changing the value — `_strip_redacted_sentinels`
# drops the leaf so deep-merge preserves whatever the overlay already had,
# rather than persisting the placeholder on top of the real secret.
_REDACTED_SENTINELS: frozenset = frozenset({"***", "<empty>"})


def _strip_redacted_sentinels(value: Any, key_hint: str = "") -> Any:
    """Recursively drop secret-keyed leaves whose value is a redaction sentinel.

    Symmetric with `_redact`: the GET handler masks secret-keyed children
    inside nested objects so the form never shows cleartext, and this
    function is the write-side counterpart that ensures the placeholder
    doesn't make a round-trip back into the overlay. Defense-in-depth
    alongside the client-side `scrubRedactedSecrets` in
    `admin_server_config.html` — an API caller (CLI / script) that forgets
    to scrub still can't corrupt secrets via this endpoint.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k) and isinstance(v, str) and v in _REDACTED_SENTINELS:
                continue
            out[k] = _strip_redacted_sentinels(v, k)
        return out
    if isinstance(value, list):
        return [_strip_redacted_sentinels(item, key_hint) for item in value]
    return value


def _redact(value: Any, key_hint: str = "") -> Any:
    """Recursively mask secret-looking fields in a config subtree.

    `key_hint` is the parent key — used so a string value like
    ``"${KEBOOLA_TOKEN}"`` under ``token_env`` is masked even though the
    value itself isn't a credential, because the key signals it points at
    one.
    """
    if isinstance(value, dict):
        return {k: (_mask(v) if _is_secret_key(k) else _redact(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key_hint) for item in value]
    if key_hint and _is_secret_key(key_hint):
        return _mask(value)
    return value


def _diff_dicts(before: dict, after: dict, path: str = "") -> List[Dict[str, Any]]:
    """Flat list of changed fields between two dicts.

    Output: [{"path": "email.smtp_host", "before": "...", "after": "..."}].
    Diff is computed on RAW values, then each row's `before`/`after` is
    masked via `_mask` when the leaf key matches `_is_secret_key` — pre-
    masking the inputs would collapse a secret rotation (e.g. password A
    → password B) into "no diff" because both sides redact to ``"***"``,
    and the audit log would then silently fail to record one of the most
    security-relevant changes. Compare raw, redact when emitting.

    Recurses into a dict on either side (treating the missing side as
    `{}`) so adding a brand-new section reports per-field paths
    (`email.smtp_host`) rather than a single opaque `email` blob — that
    keeps the audit row useful when an admin populates a section for the
    first time.
    """
    changes: List[Dict[str, Any]] = []
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        new_path = f"{path}.{key}" if path else key
        b_val = before.get(key)
        a_val = after.get(key)
        b_is_dict = isinstance(b_val, dict)
        a_is_dict = isinstance(a_val, dict)
        # Dict-vs-dict (or dict-vs-None) → recurse for per-field paths.
        if b_is_dict and a_is_dict:
            changes.extend(_diff_dicts(b_val, a_val, new_path))
        elif b_is_dict and a_val is None:
            changes.extend(_diff_dicts(b_val, {}, new_path))
        elif a_is_dict and b_val is None:
            changes.extend(_diff_dicts({}, a_val, new_path))
        # Dict↔scalar shape change is recorded as a single replacement at
        # the parent path. Recursing with `{}` would lose the scalar side
        # entirely (admin sets `keboola: {…}` to `keboola: "disabled"` —
        # auditor would see members removed but never the new value).
        # The dict side may itself contain secret-keyed children (e.g.
        # `keboola: {token_env: ${KEBOOLA_TOKEN}}` resolved to cleartext);
        # `_redact` masks those children even when the parent key isn't
        # secret-named, so the audit log doesn't leak ${ENV_VAR}-resolved
        # values when a section is replaced wholesale.
        elif b_is_dict != a_is_dict:
            if _is_secret_key(key):
                changes.append({
                    "path": new_path,
                    "before": _mask(b_val),
                    "after": _mask(a_val),
                })
            else:
                changes.append({
                    "path": new_path,
                    "before": _redact(b_val, key) if b_is_dict else b_val,
                    "after": _redact(a_val, key) if a_is_dict else a_val,
                })
        elif b_val != a_val:
            if _is_secret_key(key):
                changes.append({
                    "path": new_path,
                    "before": _mask(b_val),
                    "after": _mask(a_val),
                })
            else:
                changes.append({"path": new_path, "before": b_val, "after": a_val})
    return changes


def _deep_merge(base: dict, patch: dict) -> dict:
    """Merge `patch` into `base` recursively, returning a new dict.

    Patch values overwrite base values. Dict-into-dict recurses; everything
    else (lists, scalars, None) is replaced wholesale — admin sets
    ``email: {smtp_port: 465}`` and we don't try to re-merge nested ports.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_current_instance_yaml() -> dict:
    """Return the editor's view of instance.yaml — deep-merge of static +
    overlay via ``app.instance_config.load_instance_config``.

    Readers (GET /server-config) hit the cache and trust that writers
    invalidate. Writers must call ``reset_cache()`` explicitly *before*
    the read so they see the latest disk state in the read-modify-write
    sequence. The shared helper is the authoritative source so the editor
    never sees a different view than the rest of the running app.
    """
    from app.instance_config import load_instance_config
    return load_instance_config()


def _public_view(config: dict) -> dict:
    """Return a config dict safe to render in the admin UI form.

    Deep-copies and redacts secret-looking fields so an admin can see
    *which* fields are populated without the cleartext leaking into the
    rendered HTML / browser DevTools.
    """
    import copy
    return _redact(copy.deepcopy(config))


class ServerConfigUpdateRequest(BaseModel):
    """Patch payload for POST /api/admin/server-config.

    Only the sections listed in `_EDITABLE_SECTIONS` are accepted; anything
    else is rejected with 400. `confirm_danger` must be true if the patch
    touches any danger-zone section (auth.*, server.*).
    """
    sections: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-section patch dict (e.g. {'instance': {'name': 'X'}})",
    )
    confirm_danger: bool = Field(
        default=False,
        description="Must be true to apply changes touching auth.* or server.*",
    )


@router.get("/server-config")
async def get_server_config(
    user: dict = Depends(require_admin),
):
    """Return the current instance.yaml with secrets redacted.

    Used by the /admin/server-config UI to prefill its form. The redacted
    payload mirrors the actual file shape, so the UI doesn't need to know
    the schema — it iterates over the editable sections and renders the
    fields it finds. Empty sections still show in the response so the form
    knows to render their headers.
    """
    config = _load_current_instance_yaml()
    redacted = _public_view(config)
    # Surface every editable section so the UI renders them even when the
    # file omits them — operator can populate from scratch without manual
    # JSON edits.
    sections = {section: redacted.get(section, {}) for section in _EDITABLE_SECTIONS}
    return {
        "sections": sections,
        "editable_sections": list(_EDITABLE_SECTIONS),
        "danger_sections": list(_DANGER_SECTIONS),
        "secret_key_patterns": list(_SECRET_KEY_PATTERNS),
    }


@router.post("/server-config")
async def update_server_config(
    request: ServerConfigUpdateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Patch instance.yaml from the /admin/server-config editor.

    Accepts a partial patch keyed by section. Validates sections, refuses
    danger-zone edits without explicit confirmation, deep-merges into the
    current overlay, writes the file, and emits one audit entry per save
    with a sanitized diff. Returns ``restart_required=true`` so the UI can
    show the restart banner — hot-reload is a separate issue (see #91 Out
    of scope).
    """
    import yaml

    if not request.sections:
        raise HTTPException(status_code=422, detail="sections cannot be empty")

    # Reject unknown sections loudly. Without this, a typo like "thmee"
    # would silently land in the YAML root and the operator wouldn't see
    # their colour change apply.
    unknown = sorted(set(request.sections.keys()) - set(_EDITABLE_SECTIONS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown section(s): {', '.join(unknown)}. "
                   f"Editable: {', '.join(_EDITABLE_SECTIONS)}",
        )

    # Danger-zone gate. The UI shows a confirmation dialog before posting
    # with confirm_danger=true; an API caller (CLI/script) has to pass it
    # explicitly so they can't fat-finger a hostname change.
    danger_touched = sorted(set(request.sections.keys()) & set(_DANGER_SECTIONS))
    if danger_touched and not request.confirm_danger:
        raise HTTPException(
            status_code=400,
            detail=f"section(s) {', '.join(danger_touched)} require confirm_danger=true",
        )

    # SSRF protection — same gate the /configure wizard applies to
    # keboola_url, but here it covers any URL-bearing field reachable via
    # the per-section patch (e.g. data_source.keboola.stack_url).
    _validate_urls_in_patch(request.sections)

    # Defense-in-depth: scrub redaction sentinels (`***` / `<empty>`) out of
    # secret-keyed leaves in the patch before they reach the deep-merge.
    # The client form does the same scrub, but an API caller round-tripping
    # the GET payload could otherwise overwrite real overlay secrets with
    # the placeholder shown in the form.
    scrubbed_sections: Dict[str, Dict[str, Any]] = {
        section: _strip_redacted_sentinels(patch, section)
        for section, patch in request.sections.items()
    }

    # Serialize read-modify-write across concurrent admin saves. Without the
    # lock, two saves would each read the same overlay snapshot, merge their
    # disjoint patches, and the second os.replace would silently drop the
    # first patch. The lock spans the cache-invalidate → load → merge →
    # atomic-write sequence; the audit log sits outside since it operates on
    # local snapshots.
    from app.instance_config import reset_cache
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    config_path = data_dir / "state" / "instance.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with _overlay_write_lock:
        # Drop the in-process cache so we read the latest on-disk state,
        # including any update that landed from a concurrent caller before
        # we acquired the lock.
        reset_cache()
        before = _load_current_instance_yaml()

        # Deep merge — section-by-section so we never accidentally delete a
        # sibling section the patch didn't touch. Use the redaction-scrubbed
        # patch so a round-tripped GET payload can't overwrite real secrets
        # with the `***` placeholder.
        after = dict(before)
        for section, patch in scrubbed_sections.items():
            if not isinstance(patch, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"section '{section}' must be an object, got {type(patch).__name__}",
                )
            if isinstance(after.get(section), dict):
                after[section] = _deep_merge(after[section], patch)
            else:
                after[section] = patch

        # Write only the sections the user actually patched in this request.
        # Two reasons:
        #   1. Persisting the full merged config (or every editable section)
        #      would snapshot non-editable static sections into the overlay,
        #      shadowing later operator updates to those sections in the
        #      static file (`_load_current_instance_yaml` merges static + overlay,
        #      overlay wins per leaf).
        #   2. The merged config has `${ENV_VAR}` placeholders RESOLVED to the
        #      runtime values by config.loader. Writing every editable section
        #      back would persist real cleartext secrets where the static file
        #      had only env-var references — turning `smtp_password:
        #      ${SMTP_PASSWORD}` into `smtp_password: hunter2` in the overlay.
        # By writing only the sections in `request.sections` we keep both the
        # static-evolution and the env-var-placeholder properties intact.
        overlay_payload: Dict[str, Any] = {}
        if config_path.exists():
            try:
                overlay_payload = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                # A corrupt overlay used to be silently replaced — that masked
                # disk corruption / partial writes / hand-edits and dropped
                # every previously-saved section on the next save. Refuse and
                # surface so the operator can investigate.
                logger.exception("server-config: refusing to overwrite corrupt overlay at %s", config_path)
                raise HTTPException(
                    status_code=500,
                    detail=f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                           "back up and remove the file, or fix it by hand",
                ) from e
        for section, patch in scrubbed_sections.items():
            if section not in _EDITABLE_SECTIONS:
                continue
            # Deep-merge the patch into the existing overlay slot (or static-
            # backed `before` if overlay had nothing for this section). This
            # preserves any unrelated keys the operator didn't touch in this
            # request — e.g. patching `email.smtp_host` doesn't blow away the
            # `email.smtp_password: ${SMTP_PASSWORD}` reference.
            existing = overlay_payload.get(section)
            if not isinstance(existing, dict):
                existing = {}
            overlay_payload[section] = _deep_merge(existing, patch)

        # Atomic via tmp + os.replace so two concurrent admin saves can't
        # interleave bytes and produce corrupt YAML (especially harmful since
        # auth.* is editable here — half-written file → operator lockout).
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False))
        os.replace(tmp_path, config_path)
        logger.info("server-config: wrote %d section(s) to %s",
                    len(request.sections), config_path)

        # Invalidate cached instance config so subsequent reads pick up the
        # change. Hot-reload of running modules (auth providers, SMTP client)
        # is out of scope — the restart banner tells the operator to bounce.
        reset_cache()

    # Audit entry — diff is computed on RAW values then `_diff_dicts`
    # redacts each row whose leaf key matches `_is_secret_key`. Pre-
    # masking the inputs would collapse a secret rotation into "no
    # diff" because both sides redact to ``***``, hiding the most
    # security-relevant changes from the audit log. We log even if no
    # fields changed so the operator's intent (touched the page, hit
    # save) is auditable.
    diff = _diff_dicts(before, after)
    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="instance_config.update",
        resource="instance.yaml",
        params={
            "sections": sorted(request.sections.keys()),
            "danger_sections": danger_touched,
            "diff": diff,
            "diff_count": len(diff),
        },
    )

    return {
        "status": "ok",
        "restart_required": True,
        "sections_updated": sorted(request.sections.keys()),
        "diff_count": len(diff),
    }


# --- End server-config editor -----------------------------------------------


# Source types accepted by /api/admin/register-table. Anything else is
# rejected with 422 — keeps a typo'd source_type from silently landing in
# table_registry (where it would later confuse the orchestrator scan).
_VALID_SOURCE_TYPES: tuple[str, ...] = ("keboola", "bigquery", "jira", "local")

# Explicit allowlist of audit-payload keys whose values are credentials and
# must be masked. Substring-scan + ad-hoc whitelist (the previous shape) is
# fragile in two ways:
#   1. False positive: legit fields like `primary_key` get masked because
#      they contain "key" — we then need a whitelist exception, which has
#      to be kept in sync as new fields are added.
#   2. False negative: a future field like `primary_key_hash` *would* be
#      masked (defensible) but `not_actually_a_token` ALSO matches "token"
#      and gets masked unnecessarily; conversely, a brand-new credential
#      field that doesn't contain one of the patterns (`auth_material`,
#      `bearer`) silently leaks.
# Allowlist puts the burden on the developer adding a new secret-bearing
# field: they must add the literal key name here, which forces a code-
# review touch on the audit path. Audit the current Pydantic models
# (RegisterTableRequest / UpdateTableRequest / ConfigureRequest /
# ServerConfigUpdateRequest) when extending — the registry payloads don't
# currently carry credentials, but ConfigureRequest does (`keboola_token`)
# and could be routed through this sanitizer in the future.
_SECRET_FIELDS: frozenset = frozenset({
    # ConfigureRequest — POST /api/admin/configure carries Keboola creds.
    "keboola_token",
    # Generic names that have appeared in earlier iterations of admin
    # request bodies and could resurface — keep them masked defensively.
    "api_token",
    "auth_token",
    "bot_token",
    "client_secret",
    "google_client_secret",
    "google_oauth_client_secret",
    "password",
    "smtp_password",
    "webapp_secret_key",
    "bot_secret",
    # Marketplace PATs (private repos) — see src/marketplace.py.
    "marketplace_token",
    "marketplace_pat",
})


def _sanitize_for_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mask credential-bearing fields in a request payload before audit_log.

    Uses an explicit `_SECRET_FIELDS` allowlist (case-insensitive) instead
    of substring matching. The trade-off is that adding a new secret field
    requires updating the set — but that's the *point*: the test suite
    asserts `not_actually_a_token` does NOT get masked, so a substring-
    based regression would surface immediately, and a missing entry for a
    real new credential gets caught at code review of the audit path.
    """
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if k.lower() in _SECRET_FIELDS:
            out[k] = "***" if v not in (None, "") else "<empty>"
        else:
            out[k] = v
    return out


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = "full_refresh"
    # Composite primary keys are real (session-grain MSA tables key on
    # `(session_id, event_date)`, browse rows on more). The frontend sends +
    # reads this as a list; backend stores it JSON-serialized in VARCHAR.
    # A bare string is accepted for backward compat — see _normalize_primary_key.
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: str = "local"
    sync_schedule: Optional[str] = None
    profile_after_sync: bool = True

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)

    @field_validator("source_type", mode="before")
    @classmethod
    def _validate_source_type(cls, v):
        # None is tolerated for backward compat with old CLI scripts that
        # didn't set a source_type; the route resolves it later. Anything
        # else must be in the canonical list.
        if v in (None, ""):
            return v
        if v not in _VALID_SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {sorted(_VALID_SOURCE_TYPES)}, got {v!r}"
            )
        return v


def _validate_bigquery_register_payload(req: "RegisterTableRequest") -> None:
    """Enforce BQ-specific shape on a register/precheck request.

    Mutates the model: forces ``query_mode='remote'`` and
    ``profile_after_sync=False`` (per Decision 7 in #108) so a caller can't
    accidentally enqueue a parquet profiling pass for a remote view that
    has no local file. Raises HTTPException(422) for missing required
    fields and HTTPException(400) for unsafe identifiers / bogus project_id.
    """
    if not req.bucket or not req.bucket.strip():
        raise HTTPException(
            status_code=422,
            detail="bigquery: 'bucket' (BQ dataset) is required",
        )
    if not req.source_table or not req.source_table.strip():
        raise HTTPException(
            status_code=422,
            detail="bigquery: 'source_table' is required",
        )
    # No wildcard / sharded BQ tables in M1 (Decision 8).
    if "*" in (req.source_table or "") or "*" in (req.bucket or ""):
        raise HTTPException(
            status_code=400,
            detail="bigquery: wildcard / sharded tables are not supported (see #108 M3+)",
        )
    # Strict identifier on the DuckDB view name. CRITICAL: validate the RAW
    # name (the value that ``register_table`` actually persists to
    # ``table_registry.name`` and which the BQ extractor reads back as the
    # DuckDB view name at next rebuild). Earlier revisions normalized first
    # (``strip().lower().replace(" ", "_")``) and then checked, which let
    # names like ``"my table"`` pass here, get stored verbatim, and then
    # blow up inside ``_init_extract`` at view-create time — defeating the
    # whole point of fast-fail-at-register. We do NOT silently rewrite the
    # operator's name; if they typed ``"my table"``, return 400 with a
    # clear message and let them retype with a corrected name.
    raw_name = req.name or ""
    if raw_name.strip() != raw_name or not _is_safe_identifier(raw_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: view name {raw_name!r} is unsafe — must match "
                f"^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$ (DuckDB identifier rules) "
                "with no leading/trailing whitespace"
            ),
        )
    if not _is_safe_quoted_identifier(req.bucket.strip()):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: dataset {req.bucket!r} is unsafe (only [A-Za-z0-9_.-] allowed)",
        )
    if not _is_safe_quoted_identifier(req.source_table.strip()):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: source_table {req.source_table!r} is unsafe (only [A-Za-z0-9_.-] allowed)",
        )
    # Pull project from instance.yaml — single-project model in M1
    # (Decision: no per-table project field). Validate the format here so
    # we surface a config issue at registration rather than at first
    # rebuild, where the operator no longer has a request to look at.
    from app.instance_config import get_value
    project_id = get_value("data_source", "bigquery", "project", default="")
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "bigquery: data_source.bigquery.project is not set in instance.yaml; "
                "configure it via /admin/server-config or /api/admin/configure first"
            ),
        )
    if not _is_safe_project_id(project_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: data_source.bigquery.project {project_id!r} is malformed — "
                "must match GCP project_id grammar ^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
            ),
        )
    # Force the BQ-required mode + flag (Decision 7). The orchestrator and
    # extractor both assume remote; persisting `local` here would later create
    # a profiling job against a non-existent parquet file.
    req.query_mode = "remote"
    req.profile_after_sync = False


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = None
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: Optional[str] = None
    sync_schedule: Optional[str] = None
    profile_after_sync: Optional[bool] = None

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)


class ConfigureRequest(BaseModel):
    data_source: str  # "keboola" | "bigquery" | "local"
    keboola_token: Optional[str] = None
    keboola_url: Optional[str] = None
    bigquery_project: Optional[str] = None
    bigquery_location: Optional[str] = None
    instance_name: Optional[str] = None
    allowed_domain: Optional[str] = None


@router.get("/discover-tables")
async def discover_tables(
    user: dict = Depends(require_admin),
):
    """Discover all available tables from the configured data source."""
    try:
        from app.instance_config import get_data_source_type
        source_type = get_data_source_type()

        if source_type == "keboola":
            from connectors.keboola.client import KeboolaClient
            from app.instance_config import get_value
            url = get_value("data_source", "keboola", "stack_url", default="")
            token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
            token = os.environ.get(token_env, "") if token_env else ""
            if not token:
                token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
            client = KeboolaClient(token=token, url=url)
            tables = client.discover_all_tables()
            return {"tables": tables, "count": len(tables), "source": "keboola"}
        else:
            return {"tables": [], "count": 0, "source": source_type, "error": "Discovery not implemented for this source"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e}")


@router.get("/registry")
async def list_registry(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry."""
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    return {"tables": tables, "count": len(tables)}


# Wall-clock budget for the synchronous BQ materialization that runs after
# a successful BQ register. If the rebuild + view creation exceeds this,
# we hand the rest off to BackgroundTasks and return 202. 5s matches the
# UX contract in #108 ("Queryable as <view> within seconds") — long enough
# to cover a healthy GCE round-trip, short enough that a hung GCE call
# doesn't park the request handler.
_BQ_SYNC_REGISTER_TIMEOUT_S: float = 5.0


def _materialize_bigquery_extract() -> Dict[str, Any]:
    """Re-build the BigQuery extract.duckdb + master views.

    Wrapper used by both the synchronous (in-band) and async (BackgroundTask)
    code paths after a BQ register/update/delete. Imports kept inside the
    function so non-BQ instances don't pay the import cost on app start.

    Opens a FRESH system DB connection rather than reusing the request-scoped
    one. The request handler closes its connection in a `finally` after the
    response, but BackgroundTask + the timeout-fallback daemon thread can
    both outlive that close — they would then operate on a closed handle (or
    one being torn down concurrently). A fresh handle is cheap (DuckDB is an
    embedded engine) and isolates the worker's lifetime from the request's.

    Returns the rebuild result dict (``{"errors": [...], "tables_registered":
    N, ...}``) so the synchronous caller can propagate failures to the
    operator. Background-task callers ignore the return value, but the loud
    log inside ``_run_bigquery_materialize_with_timeout`` covers that path.
    """
    from connectors.bigquery import extractor as _bq_extractor
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator

    fresh_conn = get_system_db()
    try:
        result = _bq_extractor.rebuild_from_registry(conn=fresh_conn)
        SyncOrchestrator().rebuild()
        return result or {}
    finally:
        try:
            fresh_conn.close()
        except Exception:
            pass


def _materialize_bigquery_extract_bg() -> None:
    """BackgroundTask wrapper around `_materialize_bigquery_extract`.

    BackgroundTasks discard return values, but `rebuild_from_registry` can
    surface auth / config / identifier errors via the ``errors`` list. Log
    those at ERROR level so the failure is loud in the operator's logs even
    though the 202 response can't carry the detail (Decision 3 in #108: a
    202 is documented as "accepted, may not be queryable yet" — we don't
    block on it but we shouldn't swallow it either).
    """
    try:
        result = _materialize_bigquery_extract()
    except Exception:
        logger.exception("BQ post-register background materialize crashed")
        return
    errors = (result or {}).get("errors") or []
    if errors:
        logger.error(
            "BQ post-register background materialize completed with %d error(s): %s",
            len(errors), errors,
        )


def _run_bigquery_materialize_with_timeout(
    background: BackgroundTasks,
) -> Dict[str, Any]:
    """Try to materialize synchronously within the wall-clock budget.

    Returns a dict with:
      - ``status`` ∈ {"ok", "errors", "timeout"} — caller maps to HTTP code
      - ``errors``: list of {table, error} surfaced by ``rebuild_from_registry``
        (only present on ``status="errors"``)

    Mapping by caller (`register_table`):
      - "ok"       → 200 (synchronous success)
      - "errors"   → 500 (rebuild ran but reported errors — propagate so
                     the operator knows the registry row exists but the
                     view wasn't created)
      - "timeout"  → 202 (rebuild still running on a BackgroundTask)

    The synchronous worker runs on a daemon thread (so a hung GCE call
    can't park the request) that opens its OWN system DB connection (see
    `_materialize_bigquery_extract`). Even though FastAPI now invokes the
    sync route in a threadpool — and `done.wait()` no longer blocks the
    event loop — we still off-load to a daemon so the wait is bounded
    even if `rebuild_from_registry` ignores its own timeouts.
    """
    import threading

    done = threading.Event()
    err_holder: Dict[str, Any] = {}
    result_holder: Dict[str, Any] = {}

    def _worker():
        try:
            result_holder["result"] = _materialize_bigquery_extract()
        except Exception as e:  # pragma: no cover — logged below
            err_holder["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True, name="bq-register-rebuild")
    t.start()
    finished = done.wait(_BQ_SYNC_REGISTER_TIMEOUT_S)

    if finished:
        if "error" in err_holder:
            # Worker finished within the wall-clock budget but raised. This
            # is a HARD ERROR, not a timeout — surface it as such so the
            # operator gets the actual exception in the 500 body instead
            # of a misleading 202 + "still working in the background".
            # Earlier revisions returned ``{"status": "timeout"}`` here,
            # which the register handler then mapped to 202 + a retry
            # BackgroundTask; that hid the real failure for `_BQ_SYNC_
            # REGISTER_TIMEOUT_S` seconds before the BG retry surfaced
            # the same exception in the logs.
            exc = err_holder["error"]
            logger.error(
                "BQ post-register rebuild raised within budget: %r",
                exc,
            )
            return {
                "status": "errors",
                "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
        # Synchronous worker finished cleanly — but check whether
        # `rebuild_from_registry` itself surfaced any errors (auth fail,
        # missing project from the overlay, unsafe identifier slipping the
        # validator, etc.). Without this, those errors got silently logged
        # and the API claimed success.
        result = result_holder.get("result") or {}
        errors = result.get("errors") or []
        if errors:
            logger.error(
                "BQ post-register rebuild reported %d error(s): %s",
                len(errors), errors,
            )
            return {"status": "errors", "errors": errors}
        return {"status": "ok"}

    # Timed out — let the worker keep running on its thread (already daemon)
    # and also schedule a BackgroundTask so the orchestrator gets called via
    # the supported FastAPI path. `_INIT_EXTRACT_LOCK` in the BQ extractor
    # serializes the two file-swap calls so the slow daemon thread and the
    # background task can't tear `extract.duckdb`; the orchestrator's own
    # `_rebuild_lock` protects the master-view rebuild step downstream.
    logger.info(
        "BQ post-register rebuild exceeded %ss budget — handing off to BackgroundTask",
        _BQ_SYNC_REGISTER_TIMEOUT_S,
    )
    background.add_task(_materialize_bigquery_extract_bg)
    return {"status": "timeout"}


@router.post(
    "/register-table",
    responses={
        200: {"description": "BigQuery row registered + materialized synchronously"},
        201: {"description": "Non-BigQuery row registered (no post-insert materialize)"},
        202: {"description": "BigQuery row registered; materialize continues in background"},
        409: {"description": "Table id or view name already in use"},
        500: {"description": "BigQuery row registered but post-insert rebuild failed"},
    },
)
def register_table(
    request: RegisterTableRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new table in the system.

    Behavior by source_type:
    - **bigquery**: validates BQ-specific shape (dataset / source_table /
      identifier safety / project_id format), forces query_mode='remote' and
      profile_after_sync=False, then synchronously rebuilds extract.duckdb +
      master views with a wall-clock budget. Returns 200 with the view name
      on success, 202 on budget overrun (rebuild continues in a
      BackgroundTask), or 500 if the synchronous rebuild ran but reported
      an error (e.g. auth failure, missing project, unsafe identifier).
    - other source types: insert-only, no post-register hook. Returns 201.

    Defined as a plain ``def`` (not ``async def``) so FastAPI runs it in a
    threadpool — the synchronous-materialize path waits on
    ``threading.Event.wait()``, which would otherwise block the asyncio
    event loop and stall every other request for up to ``_BQ_SYNC_REGISTER_
    TIMEOUT_S``. ``Depends(...)``, ``BackgroundTasks``, and
    ``JSONResponse`` all work the same in sync handlers; the rest of the
    admin module mixes both styles already.

    The route does NOT carry a default ``status_code`` — each branch returns
    its own JSONResponse with the right code. A blanket ``status_code=201``
    on the decorator would mislead OpenAPI consumers about the BQ branch.

    Always: 409 on view-name collision against the existing registry, audit
    log entry on success.
    """
    from fastapi.responses import JSONResponse
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")
    repo = TableRegistryRepository(conn)
    table_id = request.name.strip().lower().replace(" ", "_")

    if repo.get(table_id):
        raise HTTPException(status_code=409, detail=f"Table '{table_id}' already registered")

    # View-name collision pre-check — distinct from id collision above.
    # `id` is derived from `name`, but two callers could legally pick
    # different display names that lower-case + slugify to the same view
    # (e.g. "Orders v2" + "orders_v2"); the strict view-name uniqueness
    # check stops that here, before the orchestrator surfaces it as a
    # silent overwrite at next rebuild.
    existing_by_name = next(
        (r for r in repo.list_all() if (r.get("name") or "") == request.name),
        None,
    )
    if existing_by_name is not None:
        raise HTTPException(
            status_code=409,
            detail=f"View name '{request.name}' is already in use by table id '{existing_by_name.get('id')}'",
        )

    # BQ rows go through the extra validation + post-insert materialization
    # contract from issue #108. Other source types keep the legacy insert-only
    # flow — Keboola materialization happens via the scheduled sync, Jira via
    # webhook, local via a manual extractor run.
    is_bigquery = request.source_type == "bigquery"
    if is_bigquery:
        _validate_bigquery_register_payload(request)

    repo.register(
        id=table_id,
        name=request.name,
        folder=request.folder,
        sync_strategy=request.sync_strategy,
        primary_key=request.primary_key,
        description=request.description,
        registered_by=user.get("email"),
        source_type=request.source_type,
        bucket=request.bucket,
        source_table=request.source_table,
        query_mode=request.query_mode,
        sync_schedule=request.sync_schedule,
        profile_after_sync=request.profile_after_sync,
    )

    # Audit entry — masked params; description kept raw (it's documentation).
    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="register_table",
        resource=table_id,
        params=_sanitize_for_audit(request.model_dump()),
    )

    if not is_bigquery:
        # Keboola / Jira / local rows are insert-only here. 201 Created — the
        # decorator no longer carries a default status, so each branch is
        # explicit about its code (BQ branch overrides via JSONResponse).
        return JSONResponse(
            status_code=201,
            content={"id": table_id, "name": request.name, "status": "registered"},
        )

    # BQ post-register: rebuild extract + master views, with timeout fallback.
    # Decision 1: 200 on synchronous success, 202 on timeout, 500 if the
    # synchronous rebuild surfaced errors. Distinct from the 201 Keboola
    # path above, so the BQ branch builds its own response.
    outcome = _run_bigquery_materialize_with_timeout(background)
    status = outcome.get("status")
    if status == "ok":
        return JSONResponse(
            status_code=200,
            content={
                "id": table_id,
                "name": request.name,
                "status": "ok",
                "view_name": table_id,
            },
        )
    if status == "errors":
        # Registry insert succeeded but the post-insert rebuild reported
        # errors — the row is in the registry but the master view was NOT
        # created. Surface the failure verbatim so the operator can fix
        # the underlying config (typically a missing
        # `data_source.bigquery.project` in the overlay or auth that lacks
        # bigquery.metadata.get on the dataset). The row stays in the
        # registry; a re-run after fixing the config picks up the existing
        # row and creates the view on the next register/update or
        # scheduler tick.
        return JSONResponse(
            status_code=500,
            content={
                "id": table_id,
                "name": request.name,
                "status": "rebuild_failed",
                "view_name": table_id,
                "errors": outcome.get("errors") or [],
                "message": (
                    "Registry row created but post-insert rebuild failed; "
                    "view is not queryable. See `errors` for details."
                ),
            },
        )
    # Default: timeout — rebuild continues on a BackgroundTask.
    return JSONResponse(
        status_code=202,
        content={
            "id": table_id,
            "name": request.name,
            "status": "accepted",
            "view_name": table_id,
            "message": "Registration accepted; materializing in background",
        },
    )


class PrecheckResponse(BaseModel):
    """Response model for /api/admin/register-table/precheck.

    Documented here so OpenAPI consumers know what to expect; the route
    returns a plain dict for backwards compatibility with the rest of the
    admin API which doesn't use response_model.
    """
    ok: bool
    table: Dict[str, Any]


@router.post("/register-table/precheck")
def register_table_precheck(
    request: RegisterTableRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Validate a register-table payload + (BQ only) confirm the source table exists.

    No DB write. Used by the UI to surface row count + size + column count
    in the modal before the operator clicks Register, and by the CLI's
    ``--dry-run`` to print what *would* be registered without touching
    state. Identical Pydantic validation to register-table; for BQ rows we
    additionally make a ``bigquery.Client(project).get_table(...)`` call
    and surface the GCP error verbatim.

    Defined as a plain ``def`` (not ``async def``) so FastAPI runs it in a
    threadpool — the BQ branch makes synchronous ``bigquery.Client(...)``
    /``client.get_table(...)`` calls, which would otherwise block the
    asyncio event loop and stall every other request for the duration of
    the GCE round-trip. Mirrors the same conversion done for
    ``register_table`` (see comment on that route). ``Depends(...)`` works
    identically in sync handlers.
    """
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")

    if request.source_type != "bigquery":
        # M1 only adds BQ-specific precheck. Other source types get a
        # validation-only response so the CLI / UI can rely on the same
        # endpoint shape across types.
        return {
            "ok": True,
            "table": {
                "name": request.name,
                "source_type": request.source_type,
                "bucket": request.bucket,
                "source_table": request.source_table,
                "rows": None,
                "size_bytes": None,
                "columns": [],
                "note": "precheck for non-bigquery sources is validation-only in M1",
            },
        }

    # BQ-specific shape validation (forces query_mode/profile_after_sync,
    # checks identifier safety, validates project_id from instance.yaml).
    _validate_bigquery_register_payload(request)

    # Round-trip the BQ jobs API to confirm the table exists and the SA can
    # see it. Imports kept local to avoid pulling google-cloud-bigquery into
    # the import chain on non-BQ instances.
    try:
        from google.cloud import bigquery  # noqa: PLC0415
        from google.api_core import exceptions as google_exc  # noqa: PLC0415
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "google-cloud-bigquery not installed; install the bigquery "
                f"extras to use BQ precheck ({e})"
            ),
        ) from e

    from app.instance_config import get_value
    project_id = get_value("data_source", "bigquery", "project", default="")
    dataset = (request.bucket or "").strip()
    source_table = (request.source_table or "").strip()
    fq = f"{project_id}.{dataset}.{source_table}"

    try:
        client = bigquery.Client(project=project_id)
        bq_table = client.get_table(fq)
    except google_exc.NotFound as e:
        raise HTTPException(status_code=404, detail=f"BigQuery table not found: {fq} ({e})") from e
    except google_exc.Forbidden as e:
        raise HTTPException(
            status_code=403,
            detail=(
                f"BigQuery access denied for {fq}: {e}. "
                "Service account needs bigquery.metadata.get on the dataset."
            ),
        ) from e
    except Exception as e:
        # Auth errors, transient 5xx, malformed table refs — surface as 400
        # so the operator gets the GCP error verbatim and can fix their
        # config without us guessing the right HTTP code.
        raise HTTPException(status_code=400, detail=f"BigQuery precheck failed for {fq}: {e}") from e

    columns = [
        {"name": f.name, "type": f.field_type}
        for f in (bq_table.schema or [])
    ]
    return {
        "ok": True,
        "table": {
            "name": request.name,
            "source_type": "bigquery",
            "bucket": dataset,
            "source_table": source_table,
            "project_id": project_id,
            "rows": int(bq_table.num_rows or 0),
            "size_bytes": int(bq_table.num_bytes or 0),
            "columns": columns,
            "column_count": len(columns),
        },
    }


@router.put("/registry/{table_id}")
async def update_table(
    table_id: str,
    request: UpdateTableRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update a registered table's configuration.

    For BQ rows, schedules a background rebuild so the master view picks
    up changes (e.g. a renamed dataset) without waiting for the next
    scheduled sync.
    """
    repo = TableRegistryRepository(conn)
    existing = repo.get(table_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Table not found")

    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    # Run BQ-shape validation BEFORE persisting whenever the merged record
    # would be a bigquery row (existing was BQ, or the patch flips it to BQ,
    # or the patch touches BQ-relevant fields on an already-BQ row). Without
    # this gate, an admin could PUT `bucket="evil\"; DROP --"` onto a BQ
    # row and the next rebuild would silently fail at view-create time —
    # surface the bad shape at PUT time instead.
    if updates:
        merged = {k: v for k, v in existing.items() if k != "registered_at"}
        merged.update(updates)
        merged.pop("id", None)  # avoid duplicate id kwarg

        if merged.get("source_type") == "bigquery":
            # Reuse the register-time validator. It mutates the request to
            # force query_mode='remote' / profile_after_sync=False — apply
            # the same coercion to `merged` so the persisted row matches.
            synthetic = RegisterTableRequest(
                name=merged.get("name") or table_id,
                bucket=merged.get("bucket"),
                source_table=merged.get("source_table"),
                source_type="bigquery",
                query_mode=merged.get("query_mode") or "remote",
                profile_after_sync=bool(merged.get("profile_after_sync") or False),
                primary_key=merged.get("primary_key"),
                description=merged.get("description"),
                folder=merged.get("folder"),
                sync_strategy=merged.get("sync_strategy") or "full_refresh",
                sync_schedule=merged.get("sync_schedule"),
            )
            _validate_bigquery_register_payload(synthetic)
            merged["query_mode"] = synthetic.query_mode
            merged["profile_after_sync"] = synthetic.profile_after_sync

        repo.register(id=table_id, **merged)

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="update_table",
        resource=table_id,
        params=_sanitize_for_audit({"updated_fields": sorted(updates.keys()), **updates}),
    )

    # If we updated a BQ row (or one that's now BQ), refresh the extract in
    # the background so the view picks up renames / column-list changes.
    # Use the BG wrapper so any rebuild errors are logged at ERROR level
    # instead of being silently dropped by BackgroundTasks (which discards
    # return values).
    after = repo.get(table_id) or {}
    if after.get("source_type") == "bigquery":
        background.add_task(_materialize_bigquery_extract_bg)

    return {"id": table_id, "updated": list(updates.keys())}


@router.delete("/registry/{table_id}", status_code=204)
async def unregister_table(
    table_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unregister a table from the system.

    For BQ rows, schedules a background rebuild so the dropped row's
    master view is removed from analytics.duckdb (rather than hanging
    around until the next scheduled sync).
    """
    repo = TableRegistryRepository(conn)
    existing = repo.get(table_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Table not found")

    was_bigquery = existing.get("source_type") == "bigquery"
    repo.unregister(table_id)

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="unregister_table",
        resource=table_id,
        params=_sanitize_for_audit({
            "name": existing.get("name"),
            "source_type": existing.get("source_type"),
            "bucket": existing.get("bucket"),
            "source_table": existing.get("source_table"),
        }),
    )

    if was_bigquery:
        background.add_task(_materialize_bigquery_extract_bg)


@router.post("/configure")
async def configure_instance(
    request: ConfigureRequest,
    user: dict = Depends(require_admin),
):
    """Configure data source and instance settings via API.

    Writes config to instance.yaml and persists secrets to .env_overlay.
    AI agents and the /setup wizard use this instead of manual file editing.
    """
    import yaml

    if request.data_source not in ("keboola", "bigquery", "local"):
        raise HTTPException(status_code=400, detail="data_source must be 'keboola', 'bigquery', or 'local'")

    # Validate credentials if provided
    if request.data_source == "keboola":
        if not request.keboola_token or not request.keboola_url:
            raise HTTPException(status_code=400, detail="keboola_token and keboola_url are required for Keboola data source")
        _validate_url_not_private(request.keboola_url, field_name="keboola_url")
        try:
            from connectors.keboola.client import KeboolaClient
            client = KeboolaClient(token=request.keboola_token, url=request.keboola_url)
            client.test_connection()
        except Exception as e:
            logger.error("Keboola connection validation failed: %s", e)
            raise HTTPException(status_code=400, detail="Keboola connection failed. Check your token and URL.")

    elif request.data_source == "bigquery":
        if not request.bigquery_project:
            raise HTTPException(status_code=400, detail="bigquery_project is required for BigQuery data source")

    # Write instance.yaml to DATA_DIR/state/ (writable Docker volume),
    # NOT to CONFIG_DIR which is mounted read-only in Docker.
    #
    # Narrow-overlay write strategy — must match `/api/admin/server-config`:
    # 1. Read overlay verbatim (do NOT fall back to static). Falling back
    #    would copy env-resolved cleartext secrets from the merged static
    #    file back into the overlay (e.g. `smtp_password: ${SMTP_PASSWORD}`
    #    → `smtp_password: hunter2`). The wizard only ever sets
    #    `instance`, `auth`, `data_source` here, so other sections must
    #    flow from the static file via `load_instance_config`'s deep-merge
    #    — they don't belong in the overlay at all.
    # 2. Patch only the sections this endpoint touches.
    # 3. Write the narrow overlay back atomically (tmp + os.replace).
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    config_path = data_dir / "state" / "instance.yaml"

    # Same serialization + corrupt-overlay handling as POST /server-config.
    with _overlay_write_lock:
        overlay: dict = {}
        if config_path.exists():
            try:
                overlay = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                logger.exception("configure: refusing to overwrite corrupt overlay at %s", config_path)
                raise HTTPException(
                    status_code=500,
                    detail=f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                           "back up and remove the file, or fix it by hand",
                ) from e

        # Merge instance settings into the overlay only — never seed from the
        # env-resolved merged config.
        if request.instance_name:
            overlay.setdefault("instance", {})["name"] = request.instance_name

        if request.allowed_domain:
            overlay.setdefault("auth", {})["allowed_domain"] = request.allowed_domain

        # data_source is fully owned by this endpoint — replace wholesale.
        overlay["data_source"] = {"type": request.data_source}
        if request.data_source == "keboola":
            overlay["data_source"]["keboola"] = {
                "stack_url": request.keboola_url,
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            }
        elif request.data_source == "bigquery":
            overlay["data_source"]["bigquery"] = {
                "project": request.bigquery_project,
                "location": request.bigquery_location or "us",
            }

        # Atomic write to writable data volume — same tmp + os.replace pattern
        # as the server-config editor so a concurrent save can't tear the file.
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay, default_flow_style=False, sort_keys=False))
        os.replace(tmp_path, config_path)
        logger.info("Wrote instance config to %s", config_path)

    # Persist secrets to .env_overlay (in data volume, never in git)
    secrets_to_persist = {}
    if request.keboola_token:
        secrets_to_persist["KEBOOLA_STORAGE_TOKEN"] = request.keboola_token
    if request.keboola_url:
        secrets_to_persist["KEBOOLA_STACK_URL"] = request.keboola_url

    if secrets_to_persist:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        overlay_path = data_dir / "state" / ".env_overlay"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing overlay
        existing_overlay = {}
        if overlay_path.exists():
            for line in overlay_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing_overlay[k.strip()] = v.strip()
        existing_overlay.update(secrets_to_persist)

        overlay_path.write_text(
            "\n".join(f"{k}={v}" for k, v in existing_overlay.items()) + "\n"
        )
        try:
            overlay_path.chmod(0o600)
        except OSError:
            pass
        logger.info("Persisted %d secrets to .env_overlay", len(secrets_to_persist))

        # Inject into current process environment
        for k, v in secrets_to_persist.items():
            os.environ[k] = v

    # Invalidate cached instance config so next read picks up changes.
    # Use the public helper (matches `/api/admin/server-config`); reaching
    # into the private global silently breaks if the cache layout changes.
    from app.instance_config import reset_cache
    reset_cache()

    return {
        "status": "ok",
        "data_source": request.data_source,
        "connection": "verified" if request.data_source != "local" else "local",
    }


def _discover_and_register_tables(conn: duckdb.DuckDBPyConnection, user_email: str) -> dict:
    """Discover tables from configured source and register them. Shared logic for API and sync."""
    from app.instance_config import get_data_source_type, get_value

    source_type = get_data_source_type()
    if source_type != "keboola":
        return {"registered": 0, "skipped": 0, "errors": 0, "tables": [], "source": source_type}

    from connectors.keboola.client import KeboolaClient
    # Read from data_source.keboola (matches what /api/admin/configure writes)
    url = get_value("data_source", "keboola", "stack_url", default="")
    token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
    token = os.environ.get(token_env, "") if token_env else ""
    if not token:
        token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

    client = KeboolaClient(token=token, url=url)
    discovered = client.discover_all_tables()

    repo = TableRegistryRepository(conn)
    registered = 0
    skipped = 0
    errors = 0
    table_names = []

    for table in discovered:
        table_id = table.get("id", "").strip().lower().replace(".", "_").replace(" ", "_")
        if not table_id:
            errors += 1
            continue

        if repo.get(table_id):
            skipped += 1
            continue

        try:
            # Parse bucket from table ID (format: in.c-bucket.table_name)
            parts = table.get("id", "").split(".")
            bucket = parts[1] if len(parts) > 1 else ""
            source_table = parts[2] if len(parts) > 2 else table.get("name", "")

            repo.register(
                id=table_id,
                name=table.get("name", table_id),
                source_type="keboola",
                bucket=bucket,
                source_table=source_table,
                query_mode="local",
                registered_by=user_email,
                description=f"Auto-discovered from Keboola: {table.get('id', '')}",
            )
            registered += 1
            table_names.append(table_id)
        except Exception as e:
            logger.warning("Failed to register %s: %s", table_id, e)
            errors += 1

    return {
        "registered": registered,
        "skipped": skipped,
        "errors": errors,
        "tables": table_names,
        "source": "keboola",
    }


@router.post("/discover-and-register")
async def discover_and_register(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Discover tables from configured source and auto-register them.

    Combines discover-tables + register-table into one call.
    Skips already-registered tables. Used by /setup wizard and AI agents.
    """
    try:
        result = _discover_and_register_tables(conn, user.get("email", "admin"))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery and registration failed: {e}")

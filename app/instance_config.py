"""Instance configuration — loads instance.yaml and exposes to FastAPI."""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_instance_config: Optional[dict] = None


def reset_cache() -> None:
    """Drop the in-process instance.yaml cache; the next ``load_instance_config``
    call re-reads from disk. Used by `/api/admin/server-config` after a save.
    Public alias so callers don't have to reach into the private global.

    Also clears ``connectors.bigquery.access.get_bq_access`` so the v2 endpoints
    pick up new BigQuery project IDs after an admin saves `instance.yaml` —
    without this, `get_bq_access`'s `@functools.cache` would freeze the projects
    at first call and require a container restart to pick up changes (Devin
    ANALYSIS_0004 on PR #138). Lazy-imported so this module stays usable in
    environments where the connectors package can't be imported (e.g. unit
    tests of instance_config in isolation)."""
    global _instance_config
    _instance_config = None
    try:
        from connectors.bigquery.access import get_bq_access
        get_bq_access.cache_clear()
    except Exception:
        # Connectors module not loaded yet, or BQ deps missing — both fine.
        pass


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge `patch` into `base`, returning a new dict.

    Dict-into-dict recurses; everything else (scalars, lists, None) is
    replaced wholesale. Used so the writable overlay can hold only the
    sections an operator has touched, while everything else flows from
    the static file unchanged. Same semantics as the helper in
    `/api/admin/server-config`'s POST handler.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_instance_config() -> dict:
    """Load instance.yaml as a deep-merge of the static file and the
    writable overlay.

    Resolution:
    1. Static base: ``CONFIG_DIR/instance.yaml`` via ``config.loader``
       (the source of truth for sections the editor doesn't expose —
       ``datasets``, ``corporate_memory``, ``openmetadata``, etc.).
    2. Overlay patch: ``DATA_DIR/state/instance.yaml`` (written by
       ``/api/admin/configure`` and ``/api/admin/server-config``;
       contains only the sections those endpoints accept).
    3. Overlay wins per-leaf via deep-merge — operator edits persist,
       static-only sections still flow through.

    Pre-2026-04-28 this function returned the overlay verbatim when it
    existed and only fell back to static when it didn't. That was a
    silent footgun: the moment someone saved any section through the
    new editor (which writes a narrow overlay by design), every
    consumer of static-only sections (corporate memory page, dataset
    list, OpenMetadata client) saw empty defaults. See PR #107.
    """
    global _instance_config
    if _instance_config is not None:
        return _instance_config

    import yaml

    # Static base — strict validation lives in config.loader.
    base: dict = {}
    try:
        from config.loader import load_instance_config as _load
        base = _load() or {}
        logger.info("Loaded instance.yaml base from config/")
    except Exception as e:
        logger.warning(f"Could not load static instance.yaml: {e}")

    # Overlay patch from the writable volume. Best-effort — a corrupt
    # overlay shouldn't take the app offline (we'd rather serve stale/base
    # config than 500 every request), but log loudly with a traceback so
    # the corruption surfaces in the operator's logs immediately. The
    # write-side endpoints (POST /api/admin/server-config and /configure)
    # refuse to overwrite a corrupt overlay with HTTP 500, so an admin
    # noticing the saves break is the second line of defence.
    #
    # ${ENV_VAR} interpolation: ``config.loader.load_instance_config`` runs
    # the static base through ``_resolve_env_refs`` already, but raw
    # ``yaml.safe_load`` here would leave overlay strings like
    # ``${ANTHROPIC_API_KEY}`` as literal placeholders. /api/admin/configure
    # writes exactly that string into the seeded ai: block (#176), so we
    # mirror the resolver here before the deep-merge — without it, the
    # LLM factory receives the literal placeholder and rejects it as an
    # invalid api key (#179 review fix).
    # Resolve via _state_dir() so the path matches the writer in
    # app/api/admin.py — under the flat-mount layout (STATE_DIR=/data-state)
    # both the configure-endpoint and the server-config-endpoint write
    # ``/data-state/instance.yaml``; reading from ``/data/state/...`` here
    # would silently load stale config from the regenerable data disk.
    from app.secrets import _state_dir
    overlay_path = _state_dir() / "instance.yaml"
    if overlay_path.exists():
        try:
            overlay = yaml.safe_load(overlay_path.read_text()) or {}
            from config.loader import _resolve_env_refs
            overlay = _resolve_env_refs(overlay)
            base = _deep_merge(base, overlay)
            logger.info("Merged overlay from %s", overlay_path)
        except Exception:
            logger.exception(
                "instance.yaml overlay at %s is corrupt — falling back to "
                "static base config; saves through the editor will refuse "
                "until the file is repaired", overlay_path,
            )

    _instance_config = base
    return _instance_config


def get_value(*keys, default=None) -> Any:
    """Get nested value from instance config."""
    config = load_instance_config()
    current = config
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def get_data_source_type() -> str:
    return os.environ.get("DATA_SOURCE", get_value("data_source", "type", default="local"))


def get_home_route() -> str:
    """Path that ``/`` redirects to for an authenticated user.

    Resolution order: ``AGNES_HOME_ROUTE`` env var (Terraform-friendly,
    overrides everything) > ``instance.home_route`` in instance.yaml >
    default ``/dashboard``. The env-overrides-yaml shape mirrors
    :func:`get_data_source_type` (precedent in this file) so operators
    can flip a fork to ``/home`` per-deployment without forking the
    YAML.

    Validated to start with ``/`` and not ``//`` so a misconfigured
    value can't pivot the root redirect to an external host.
    """
    raw = os.environ.get("AGNES_HOME_ROUTE") or get_value(
        "instance", "home_route", default="/dashboard"
    )
    route = (raw or "").strip()
    if not route.startswith("/") or route.startswith("//"):
        return "/dashboard"
    return route


def get_gws_oauth_credentials() -> dict:
    """Pre-configured Google Workspace CLI OAuth client (client_id + secret).

    When set, /home renders a connector prompt that tells the analyst (and
    Claude) to export `GOOGLE_WORKSPACE_CLI_CLIENT_ID` and
    `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET` and skip the "create your own GCP
    project" walkthrough — the operator has already provisioned a shared
    OAuth app for the instance. When unset, the prompt falls back to the
    manual `gws auth setup` flow.

    OAuth client_id + secret here are app identifiers for an installed
    "Desktop app" OAuth client, not a per-user secret. They're rendered
    into the public /home page on purpose — they identify the OAuth app,
    and the redirect-URI / scope guardrails on the GCP-side OAuth client
    are what enforce safety. Treat them like a publishable bundle ID,
    not a credential.

    Resolution order (env-overrides-yaml, mirrors :func:`get_home_route`):

    - ``AGNES_GWS_CLIENT_ID`` env > ``instance.gws.client_id`` YAML > None
    - ``AGNES_GWS_CLIENT_SECRET`` env > ``instance.gws.client_secret`` YAML > None
    - ``AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT`` env > ``instance.gws.oauthlib_insecure_transport`` YAML > "1"
      (kept as "1" by default because the gws CLI binds an HTTP loopback
       on 127.0.0.1:8080 for the OAuth redirect, and Google's oauthlib
       refuses non-HTTPS redirects without this flag).

    Both id and secret must be set for the configured branch to engage;
    a half-configured instance falls back to manual setup with a warning.
    """
    cid = os.environ.get("AGNES_GWS_CLIENT_ID") or get_value(
        "instance", "gws", "client_id", default=""
    )
    secret = os.environ.get("AGNES_GWS_CLIENT_SECRET") or get_value(
        "instance", "gws", "client_secret", default=""
    )
    insecure = os.environ.get("AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT") or get_value(
        "instance", "gws", "oauthlib_insecure_transport", default="1"
    )
    project_id = os.environ.get("AGNES_GWS_PROJECT_ID") or get_value(
        "instance", "gws", "project_id", default=""
    )
    cid = (cid or "").strip()
    secret = (secret or "").strip()
    project_id = (project_id or "").strip()
    # Derive project_id from the client_id when not explicitly set. Google's
    # OAuth client_id format is "<numeric-project-number>-<random>.apps.
    # googleusercontent.com"; the numeric prefix is required by the
    # client_secret.json schema (gws CLI's Rust struct treats it as
    # non-Option). Falls back to "" when the client_id is empty or
    # malformed; the configured branch in the template degrades gracefully.
    if not project_id and cid and "-" in cid:
        project_id = cid.split("-", 1)[0]
    return {
        "client_id": cid,
        "client_secret": secret,
        "project_id": project_id,
        "oauthlib_insecure_transport": str(insecure).strip() or "1",
        "configured": bool(cid and secret),
    }


def get_home_automode_visibility() -> bool:
    """Whether /home renders the "Step 3 — turn on auto-accept mode"
    install-block. Auto-accept mode is the recommended middle ground
    between default per-action prompting (slow) and full YOLO
    (`--dangerously-skip-permissions`, broad blast radius).

    Cautious-rollout instances can hide the section by setting
    ``AGNES_HOME_SHOW_AUTOMODE=0`` so users learn the permission flow
    first; the same content stays available on /setup-advanced.

    Resolution: env var > ``instance.home.show_automode`` YAML > True.
    Mirrors :func:`get_home_route` shape so Terraform overrides work
    the same way.
    """
    raw = os.environ.get("AGNES_HOME_SHOW_AUTOMODE")
    if raw is None:
        raw = get_value("instance", "home", "show_automode", default=True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def get_instance_name() -> str:
    return get_value("instance", "name", default="AI Data Analyst")


def get_instance_subtitle() -> str:
    return get_value("instance", "subtitle", default="")


def get_instance_admin_email() -> str:
    """Operator-facing contact address shown in user-side prompts that
    suggest the user reach out to their Agnes admin (e.g. the /home GWS
    connector tile renders an "Email admin" mailto button when no shared
    OAuth app is provisioned). Empty string when unset — the template
    branches on the value being truthy, so an empty value hides the
    button rather than rendering a broken `mailto:` link.

    Resolution: ``AGNES_INSTANCE_ADMIN_EMAIL`` env > ``instance.admin_email`` YAML > "".
    Mirrors :func:`get_home_route` shape so Terraform overrides work.
    """
    raw = os.environ.get("AGNES_INSTANCE_ADMIN_EMAIL")
    if raw is None:
        raw = get_value("instance", "admin_email", default="")
    return (raw or "").strip()


def get_sync_interval() -> str:
    """Human-readable refresh cadence shown in the analyst welcome prompt."""
    return get_value("instance", "sync_interval", default="1 hour")


def get_allowed_domains() -> list:
    domain = get_value("auth", "allowed_domain", default="")
    if domain:
        return [d.strip() for d in domain.split(",") if d.strip()]
    return []


def get_datasets() -> dict:
    return get_value("datasets", default={})


def get_theme() -> dict:
    return get_value("theme", default={})


def get_auth_config() -> dict:
    return get_value("auth", default={})


def get_corporate_memory_config() -> dict:
    return get_value("corporate_memory", default={})


def get_guardrails_config() -> dict:
    """Flea-market upload-guardrail config (see docs/STORE_GUARDRAILS.md).

    Returns the ``guardrails:`` block from instance.yaml, or an empty dict
    when not configured. Call site: ``src/store_guardrails/runner.py``.
    """
    return get_value("guardrails", default={})


def get_guardrails_review_model() -> str:
    """Resolved Anthropic model ID used for the LLM security review.

    Reads ``guardrails.review_model`` (one of ``haiku``, ``sonnet``,
    ``opus``, or a concrete ``claude-*`` model ID) and returns the
    concrete model ID. Defaults to Haiku — the cheapest tier — when the
    operator hasn't set the key. Override per-instance for higher-stakes
    review at proportionally higher cost.
    """
    from connectors.llm.factory import resolve_model_tier

    raw = get_value("guardrails", "review_model", default="haiku")
    return resolve_model_tier(raw)


def get_guardrails_blocked_quota_per_day() -> int:
    """Per-submitter cap on `blocked_inline` rows in the trailing 24h.

    Defaults to 50. Set to 0 in instance.yaml to disable the quota
    entirely (useful for trusted single-tenant deployments). Bounds the
    worst case where a bot loops on malformed ZIPs and fills disk +
    the admin queue with noise.
    """
    val = get_value("guardrails", "blocked_quota_per_day", default=50)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 50


def get_guardrails_blocked_bundle_ttl_days() -> int:
    """How many days to keep a blocked bundle's bytes on disk.

    Default 30. The submission row + sha256 + size always survive — only
    the bundle bytes get removed. ``bundle_purged_at`` is stamped so the
    detail UI renders *"Bundle purged on …"*. Set to 0 to disable the
    TTL purge entirely (bundles persist indefinitely until manual
    Delete).
    """
    val = get_value("guardrails", "blocked_bundle_ttl_days", default=30)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 30


def get_guardrails_stuck_review_grace_seconds() -> int:
    """How long a submission may stay at ``status='pending_llm'`` before
    the reaper flips it to ``review_error``.

    The BackgroundTasks worker normally writes a verdict within a few
    seconds. If the worker crashes between status flip and verdict
    write, the row would otherwise sit at pending_llm forever — admin
    queue surfaces it indefinitely; submitter never gets a verdict.

    Default 1800s (30 min) comfortably exceeds the Sonnet/Opus p99
    wall time for the configured ``MAX_REVIEW_BYTES`` payload. Set to
    0 to disable the reaper entirely.
    """
    val = get_value("guardrails", "stuck_review_grace_seconds", default=1800)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 1800


def get_guardrails_enabled() -> bool:
    """Master kill-switch for the guardrail pipeline.

    Defaults to True. Operators can disable by setting ``guardrails.enabled:
    false`` in instance.yaml — useful for local development against the
    UI without burning Anthropic tokens. Inline checks always run; this
    flag only gates the LLM step (and skips the pending → approved hold).

    Auto-fallback: when the YAML says enabled but no ANTHROPIC_API_KEY /
    LLM_API_KEY is set in the environment, behave as disabled. This
    keeps the test suite + first-boot operator experience sane — uploads
    auto-approve until the operator wires up an LLM provider rather than
    silently piling up in ``review_error``.
    """
    if not bool(get_value("guardrails", "enabled", default=True)):
        return False
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True
    if os.environ.get("LLM_API_KEY", "").strip():
        return True
    return False

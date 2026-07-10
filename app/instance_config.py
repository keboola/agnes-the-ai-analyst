"""Instance configuration — loads instance.yaml and exposes to FastAPI."""

import logging
import os
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


def get_database_config() -> dict:
    """Return ``{backend: "...", url: "..."}`` from the state machine.

    Centralised so future callers don't reach into src.db_state_machine
    directly. Cache invalidation via reset_database_cache() after
    /api/admin/db/migrate success.
    """
    from src.db_state_machine import read_backend_state

    state, url = read_backend_state()
    return {"backend": state.value, "url": url}


def reset_database_cache() -> None:
    """No-op for now — get_database_config reads fresh each call.

    Exposed as a public API so future caching (if added) has a single
    invalidation point. Called by app/api/db_state.py after a successful
    backend flip.
    """
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
                "until the file is repaired",
                overlay_path,
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


def get_slack_transport() -> str:
    """Inbound Slack transport for this instance: "http" (default) | "socket".

    Resolution: ``SLACK_TRANSPORT`` env (Terraform-friendly, overrides
    everything) > ``chat.slack.transport`` in instance.yaml > default
    ``"http"``. Unknown values fall back to ``"http"`` so a typo never
    starts a dead Socket Mode WS. Mirrors :func:`get_data_source_type`.
    """
    raw = os.environ.get("SLACK_TRANSPORT") or get_value("chat", "slack", "transport", default="http")
    value = (raw or "http").strip().lower()
    if value not in ("http", "socket"):
        return "http"
    return value


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
    raw = os.environ.get("AGNES_HOME_ROUTE") or get_value("instance", "home_route", default="/dashboard")
    route = (raw or "").strip()
    if not route.startswith("/") or route.startswith("//"):
        return "/dashboard"
    return route


def get_public_url() -> str:
    """Absolute base URL of this instance (scheme + host, no trailing slash).

    Resolution order: ``PUBLIC_URL`` env var (Terraform-friendly, overrides
    everything) > ``server.public_url`` in instance.yaml > ``""`` (unset).
    The env-overrides-yaml shape mirrors :func:`get_home_route`.

    Needed by surfaces that have no inbound HTTP request to derive the host
    from — notably the Slack bot, which runs over Socket Mode and must mint
    *absolute* ``/slack/bind`` magic links and ``/chat`` deep links. Callers
    fall back to a root-relative path when this is empty, so an unset value
    degrades gracefully rather than crashing.
    """
    raw = os.environ.get("PUBLIC_URL") or get_value("server", "public_url", default="")
    return (raw or "").strip().rstrip("/")


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
    # Resolution order: env > vault (admin UI) > instance.yaml > ""
    # Lazy import keeps app.datasource_secrets out of the module-level import
    # graph (avoids circular import via src.repositories at startup).
    try:
        from app.datasource_secrets import datasource_secret as _ds_secret
    except Exception:
        _ds_secret = lambda _n: None  # noqa: E731

    cid = (
        os.environ.get("AGNES_GWS_CLIENT_ID")
        or _ds_secret("AGNES_GWS_CLIENT_ID")
        or get_value("instance", "gws", "client_id", default="")
    )
    secret = (
        os.environ.get("AGNES_GWS_CLIENT_SECRET")
        or _ds_secret("AGNES_GWS_CLIENT_SECRET")
        or get_value("instance", "gws", "client_secret", default="")
    )
    insecure = os.environ.get("AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT") or get_value(
        "instance", "gws", "oauthlib_insecure_transport", default="1"
    )
    project_id = os.environ.get("AGNES_GWS_PROJECT_ID") or get_value("instance", "gws", "project_id", default="")
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


def get_instance_theme() -> str:
    """Active UI theme for this instance — drives the `data-theme`
    attribute on `<html>` so the design-system token set
    (`--ds-*`) flips between palettes without touching markup.

    Values:
      - ``blue``   — current default. Brand-blue hero gradient,
                     blue CTAs, translucent-white eyebrow.
      - ``navy``   — darker palette opted into via server config.
                     Dark navy hero gradient, mint-green CTAs +
                     eyebrow accents.
      - ``dark``   — full dark surface palette (navy-tinted dark
                     stack, pale-ink text); see ``[data-theme="dark"]``
                     in ``design-tokens.css``.
      - ``auto``   — light by default, flips to the ``dark`` palette
                     when the user's OS prefers dark (resolved
                     client-side in ``_theme_resolve.html``).

    Resolution: ``AGNES_INSTANCE_THEME`` env var
    (Terraform-friendly) > ``instance.theme`` in instance.yaml >
    default ``"blue"``. Unrecognised values fall back to ``"blue"``
    so a typo doesn't silently break every page.
    """
    raw = os.environ.get("AGNES_INSTANCE_THEME")
    if raw is None:
        raw = get_value("instance", "theme", default="blue")
    if not isinstance(raw, str):
        return "blue"
    value = raw.strip().lower()
    if value not in ("navy", "blue", "dark", "auto"):
        return "blue"
    return value


def get_home_automode_visibility() -> bool:
    """Whether /home renders the "Step 3 — turn on auto-accept mode"
    install-block. /home recommends launching with
    `claude --permission-mode auto`, whose classifier auto-approves
    safe actions (file edits and safe Bash) so the setup script runs
    mostly unattended while riskier commands can still prompt. The
    broader-blast-radius YOLO flag (`--dangerously-skip-permissions`)
    is no longer surfaced on /home — it stays documented as an
    advanced option on /setup-advanced.

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


def get_home_status_frame_visibility() -> bool:
    """Whether /home renders the homepage status frame (Last sync,
    Sessions, Prompts, Tokens, Projects).

    The template ALSO gates rendering on ``users.onboarded`` so a
    fresh user sees a clean install-hero before the all-zero stat
    cards. This helper is the operator-level master switch; the
    onboarding gate is a UX coherence rule layered on top.

    Cautious-rollout instances that would rather not expose token
    counters to analysts yet can disable with
    ``AGNES_HOME_SHOW_STATUS_FRAME=0`` (or
    ``instance.home.show_status_frame: false`` in YAML).

    Resolution: env var > ``instance.home.show_status_frame`` YAML > True.
    Shape mirrors :func:`get_home_automode_visibility` so Terraform
    overrides land the same way.
    """
    raw = os.environ.get("AGNES_HOME_SHOW_STATUS_FRAME")
    if raw is None:
        raw = get_value("instance", "home", "show_status_frame", default=True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def get_instance_name() -> str:
    return get_value("instance", "name", default="AI Data Analyst")


def get_instance_subtitle() -> str:
    return get_value("instance", "subtitle", default="")


def get_instance_brand() -> str:
    """Product-name brand string surfaced to end users in the analyst-facing
    UI (``/home`` hero copy, ``/setup``, ``/login``, the clipboard setup
    script, etc.). Defaults to ``"Agnes"`` — operators rebranding this OSS
    set it to e.g. ``"Foundry AI"`` without forking.

    Distinct from :func:`get_instance_name` which drives page titles and
    represents the deploying organization's display name ("AI Data Analyst").
    Brand is the *product*; name is the *deployment*.

    Resolution: ``AGNES_INSTANCE_BRAND`` env > ``instance.brand`` YAML > ``"Agnes"``.
    Mirrors :func:`get_home_route` shape so Terraform env overrides work.
    """
    raw = os.environ.get("AGNES_INSTANCE_BRAND")
    if raw is None:
        raw = get_value("instance", "brand", default="Agnes")
    value = (raw or "").strip()
    return value or "Agnes"


def get_instance_logo_svg() -> str:
    """Raw inline ``<svg>`` markup rendered into the header brand slot
    (``_app_header.html``). When non-empty, replaces the text brand in
    the header — typical use is a lockup that already contains the
    brand wordmark. When empty, the header falls back to
    :func:`get_instance_name` as text.

    Resolution: ``AGNES_INSTANCE_LOGO_SVG`` env > ``instance.logo_svg``
    YAML > ``""``. Mirrors :func:`get_instance_brand` so Terraform env
    overrides work the same way.
    """
    raw = os.environ.get("AGNES_INSTANCE_LOGO_SVG")
    if raw is None:
        raw = get_value("instance", "logo_svg", default="")
    return (raw or "").strip()


def get_instance_overview() -> str:
    """Operator-authored Overview body rendered on ``/home``. Markdown is
    NOT auto-converted — operators paste HTML (matches the existing
    ``news_intro`` ``| safe`` filter). Empty default = section hidden,
    keeping the OSS vendor-neutral when an instance ships without
    operator-specific framing.

    Resolution: ``AGNES_INSTANCE_OVERVIEW`` env > ``instance.overview``
    YAML > ``""``. Mirrors :func:`get_instance_logo_svg`.
    """
    raw = os.environ.get("AGNES_INSTANCE_OVERVIEW")
    if raw is None:
        raw = get_value("instance", "overview", default="")
    return (raw or "").strip()


def get_instance_support() -> str:
    """Operator-authored Support body rendered inside the welcome hero
    on ``/home``. Same ``| safe``-filter shape as
    :func:`get_instance_overview` — operators paste HTML. Distinct
    config field so help/chat pointers can be updated independently
    from the product framing in ``instance.overview``.

    Typical content: a one-line invitation pointing at a chat space
    (Google Chat / Slack / Teams), a mailing list, or an internal
    runbook. Empty default = block hidden, keeping the OSS
    vendor-neutral when an instance ships without an operator-defined
    support channel.

    Resolution: ``AGNES_INSTANCE_SUPPORT`` env > ``instance.support``
    YAML > ``""``. Mirrors :func:`get_instance_overview`.
    """
    raw = os.environ.get("AGNES_INSTANCE_SUPPORT")
    if raw is None:
        raw = get_value("instance", "support", default="")
    return (raw or "").strip()


def get_hidden_login_features() -> frozenset[str]:
    """Stable keys of the ``/login`` feature cards to hide on this instance.

    The ``/login`` left panel renders a fixed set of feature-card tiles, each
    tagged with a stable key (``data``, ``marketplace``, ``mcp``, ``memory``,
    ``anywhere``). Listing a key here drops that card — a generic,
    per-deployment way to trim the landing chrome without forking the
    template.

    Resolution: ``AGNES_INSTANCE_HIDE_LOGIN_FEATURES`` env (comma-separated,
    e.g. ``"mcp,memory"``) > ``instance.hide_login_features`` in instance.yaml
    (a YAML list *or* a comma-separated string) > empty. Values are split on
    commas, stripped, lowercased, de-duplicated, and empties dropped, yielding
    a ``frozenset`` the template membership-tests against.

    The empty default hides nothing — the shipped OSS renders every card, so
    the concrete choice of what to hide stays a deployment-level decision and
    the public repo stays vendor-neutral. Mirrors :func:`get_instance_overview`
    in the env-overrides-yaml shape.
    """
    raw = os.environ.get("AGNES_INSTANCE_HIDE_LOGIN_FEATURES")
    if raw is None:
        raw = get_value("instance", "hide_login_features", default="")
    # Accept a YAML list (``["mcp", "memory"]``) or a comma-separated string
    # (``"mcp, memory"``) interchangeably — split every piece on commas so
    # either form normalizes the same way.
    if isinstance(raw, (list, tuple)):
        tokens: list[str] = []
        for item in raw:
            tokens.extend(str(item).split(","))
    else:
        tokens = str(raw or "").split(",")
    return frozenset(token.strip().lower() for token in tokens if token.strip())


def get_instance_custom_preamble() -> str:
    """Operator-authored preamble injected at the TOP of the `agnes init`
    install prompt (above ``Set up the {instance_brand} CLI…``). Empty/unset
    emits zero lines so the rendered prompt stays byte-identical to the
    default — keeping the OSS vendor-neutral; the brand-specific value is
    set in production config, outside this repo.

    ``{instance_brand}`` (and the other server-side placeholders substituted
    by :func:`app.web.setup_instructions.resolve_lines`) are honored inside
    the preamble, but it MUST NOT contain literal ``{server_url}`` /
    ``{token}`` — those are only substituted at click time in the JS
    clipboard flow, not in the preamble body.

    Resolution: ``AGNES_INSTANCE_CUSTOM_PREAMBLE`` env > ``instance.custom_preamble``
    YAML > ``""``. Mirrors :func:`get_instance_overview`.
    """
    raw = os.environ.get("AGNES_INSTANCE_CUSTOM_PREAMBLE")
    if raw is None:
        raw = get_value("instance", "custom_preamble", default="")
    return (raw or "").strip()


_CUSTOM_SCRIPT_PLACEMENTS = ("head_start", "head_end", "body_end")


def _custom_script_enabled(value) -> bool:
    """Coerce the per-entry ``enabled`` field tolerant of YAML's many
    truthiness shapes.

    Operators hand-editing YAML (or pasting blocks from another source)
    can land ``enabled: "false"`` (quoted string), ``enabled: 0``, or
    ``enabled: no`` rather than the boolean ``false``. ``bool("false")``
    is ``True`` in Python, so a naive truth check silently keeps the
    script live — a footgun for what's meant to be a kill switch on
    admin-injected JS. Missing / ``None`` → live (default-on, matches
    the registered field shape).
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "no", "0", "off")
    return bool(value)


def get_custom_scripts() -> list[dict]:
    """Operator-injected HTML/JS blocks rendered by ``base.html``.

    Reads ``instance.custom_scripts`` from instance.yaml — a list of
    dicts ``{name, enabled, placement, html}``. Each block lands in one
    of three template slots:

    - ``head_start`` — first thing in ``<head>``, before any CSS/JS
      (rare; GTM dataLayer init).
    - ``head_end`` — last thing in ``<head>`` (default; analytics +
      feedback widgets like Marker.io, Sentry, Hotjar).
    - ``body_end`` — just before ``</body>`` (vendors that explicitly
      ask for bottom placement).

    Trust boundary: admin-only. ``instance.yaml`` is written through
    ``/api/admin/server-config`` (gated by ``require_admin``) and the
    rendered HTML is interpolated with ``| safe``, exactly mirroring
    ``instance.logo_svg`` / ``instance.overview``.

    Normalization:
    - Drop entries whose ``enabled`` resolves to false via
      :func:`_custom_script_enabled` (handles quoted strings, 0/1, etc.
      — not just the Python ``False`` singleton).
    - Drop entries whose ``html`` strips to empty.
    - Default missing ``name`` to "" and missing ``placement`` to
      "head_end".
    - Drop entries whose ``placement`` isn't in the allowlist, with a
      logged warning naming the offending block — admin sees the
      mistake instead of the server crashing.

    No env-var override: the structure is a list of objects, which
    doesn't round-trip cleanly through env vars; deployment-time
    injection happens by writing the YAML from the deploy script.

    Returns ``[]`` when YAML omits the key — empty by default keeps the
    OSS vendor-neutral.
    """
    raw = get_value("instance", "custom_scripts", default=None)
    if not raw:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "instance.custom_scripts must be a list, got %s — ignoring",
            type(raw).__name__,
        )
        return []
    out: list[dict] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning(
                "instance.custom_scripts[%d] must be a dict, got %s — skipping",
                idx,
                type(entry).__name__,
            )
            continue
        if not _custom_script_enabled(entry.get("enabled")):
            continue
        html = (entry.get("html") or "").strip()
        if not html:
            continue
        placement = (entry.get("placement") or "head_end").strip()
        if placement not in _CUSTOM_SCRIPT_PLACEMENTS:
            logger.warning(
                "instance.custom_scripts[%d] (name=%r) has unknown placement %r — must be one of %s — skipping",
                idx,
                entry.get("name", ""),
                placement,
                ", ".join(_CUSTOM_SCRIPT_PLACEMENTS),
            )
            continue
        out.append(
            {
                "name": str(entry.get("name") or ""),
                "enabled": True,
                "placement": placement,
                "html": html,
            }
        )
    return out


def get_workspace_dir_name() -> str:
    """Filesystem-safe folder name for the analyst's local workspace
    (``~/<workspace_dir_name>``). Defaults to :func:`get_instance_brand`
    with every non-alphanumeric character stripped, so ``"Foundry AI"``
    becomes ``"FoundryAI"`` and ``"Agnes"`` stays ``"Agnes"``.

    An explicit override exists for operators who want a folder name that
    doesn't follow the strip-whitespace derivation.

    Resolution: ``AGNES_WORKSPACE_DIR_NAME`` env > ``instance.workspace_dir``
    YAML > derived from :func:`get_instance_brand`.
    """
    raw = os.environ.get("AGNES_WORKSPACE_DIR_NAME")
    if raw is None:
        raw = get_value("instance", "workspace_dir", default="")
    explicit = (raw or "").strip()
    if explicit:
        return explicit
    import re

    derived = re.sub(r"[^A-Za-z0-9]", "", get_instance_brand())
    return derived or "Agnes"


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


def get_infra_repo_url() -> str:
    """Optional URL of the infrastructure/provisioning repository for this
    instance (e.g. the Terraform repo that deployed the VM). Used by the
    built-in ``agnes-operator`` marketplace plugin to give the operator's
    Claude a concrete pointer to where they manage infra for this instance.

    Empty string by default — the OSS distribution ships vendor-neutral;
    an operator sets this so the operator plugin can name the real infra
    repo without hardcoding anything in shipped content.

    Resolution: ``AGNES_INFRA_REPO_URL`` env > ``instance.infra_repo_url``
    YAML > ``""`` (unset). Mirrors :func:`get_instance_admin_email` shape.
    """
    raw = os.environ.get("AGNES_INFRA_REPO_URL")
    if raw is None:
        raw = get_value("instance", "infra_repo_url", default="")
    return (raw or "").strip()


def get_atlassian_base_url() -> str:
    """Operator-provisioned Atlassian Cloud site URL — baked into the
    Atlassian connector prompt so end users don't have to guess /
    paste their org's `https://<myorg>.atlassian.net`.

    When set, the connector prompt's "ask me for the site URL" step
    is replaced by a literal value the helper script substitutes
    directly. When unset (empty string), the prompt falls back to
    asking the user — same flow as today.

    Normalized: trailing slashes and a trailing ``/wiki`` are stripped
    so the value is always the bare site root. Matches the
    normalization the per-user helper script already does at storage
    time (see atlassian_prompt step 4 guard 2).

    Resolution: ``AGNES_ATLASSIAN_BASE_URL`` env > ``instance.atlassian.base_url`` YAML > "".
    Mirrors :func:`get_instance_admin_email` so Terraform overrides
    work the same way.
    """
    raw = os.environ.get("AGNES_ATLASSIAN_BASE_URL")
    if raw is None:
        raw = get_value("instance", "atlassian", "base_url", default="")
    value = (raw or "").strip().rstrip("/")
    if value.endswith("/wiki"):
        value = value[: -len("/wiki")]
    return value


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
    """Per-submitter cap on `blocked_llm` + `review_error` rows in the
    trailing 24h.

    Defaults to 50. Set to 0 in instance.yaml to disable the quota
    entirely (useful for trusted single-tenant deployments). Bounds
    the worst case where a bot loops on bundles that pass inline
    checks but trip the async LLM reviewer. Inline failures are
    hard-rejected upstream (no row created) and not counted here;
    HTTP-level rate limiting + the
    ``store.upload.security_blocked`` audit trail cover that path.
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


def get_guardrails_min_description_chars() -> int:
    """Minimum character floor for skill / agent / plugin descriptions.

    Reads ``guardrails.min_description_chars`` (default 60). Set the
    floor low (e.g. 30) to relax the inline content check; set high
    (e.g. 120) to push submitters closer to the Claude-skill-ecosystem
    norm of 150–220 chars per description.
    """
    val = get_value("guardrails", "min_description_chars", default=60)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 60


def get_guardrails_min_command_description_chars() -> int:
    """Minimum character floor for slash-command descriptions.

    Reads ``guardrails.min_command_description_chars`` (default 25).
    Commands are typically one-verb actions — kept tighter than skills.
    """
    val = get_value("guardrails", "min_command_description_chars", default=25)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 25


def get_guardrails_min_distinct_words() -> int:
    """Minimum distinct-word count for any description string.

    Reads ``guardrails.min_distinct_words`` (default 5). Defends against
    "padding hits the char count but says nothing" cases like
    `"description description description description"`.
    """
    val = get_value("guardrails", "min_distinct_words", default=5)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 5


def get_guardrails_min_body_chars() -> int:
    """Minimum body-content floor for skill / agent files.

    Reads ``guardrails.min_body_chars`` (default 200). Body = the
    markdown after the YAML frontmatter. 200 chars is a "one paragraph"
    floor that catches stubs; real skill bodies run 500–2000 chars.
    """
    val = get_value("guardrails", "min_body_chars", default=200)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 200


def get_guardrails_enabled() -> bool:
    """Operator's stated intent for the guardrail pipeline.

    Reads ``guardrails.enabled`` from instance.yaml. Defaults to True.
    Operators can explicitly disable by setting ``guardrails.enabled:
    false`` — useful for local development against the UI without
    burning Anthropic tokens.

    Note: this returns intent ONLY. Whether the LLM provider has
    working credentials is a separate concern — see
    :func:`get_guardrails_llm_provider_ready`. The two are kept apart
    so callers can implement fail-CLOSED behavior: hold submissions at
    ``pending_llm`` (instead of silently auto-approving) when intent is
    True but credentials are missing.
    """
    return bool(get_value("guardrails", "enabled", default=True))


def get_guardrails_llm_provider_ready() -> bool:
    """Whether the LLM provider has credentials present in the
    environment.

    Independent from :func:`get_guardrails_enabled` (operator intent).
    A False return here when intent is True is a misconfiguration —
    the caller should hold submissions at ``pending_llm`` and surface
    a loud boot-time warning rather than silently auto-approving.
    """
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True
    if os.environ.get("LLM_API_KEY", "").strip():
        return True
    return False

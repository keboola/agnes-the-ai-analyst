"""Builders for the upstream python-js runtime contract.

The runtime image reads /data/config.json (dataApp.git + dataApp.secrets) and
never sees the platform: DATA_LOADER_API_URL stays unset by design (spec §2).
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

# Slugs that must never be assignable to a data app: each one is a literal
# path segment the web UI (`app/web/router.py`'s `apps_web_router`) or the
# ingress proxy (`app/api/data_apps_proxy.py`) registers directly under
# `/apps/`. A data app named "detail" would collide with the
# `GET /apps/detail/{slug}` web route — its own sub-paths (e.g.
# `/apps/detail/style.css`) would be swallowed by that route instead of
# reaching the proxy. Add any future literal `/apps/<segment>` route here.
RESERVED_SLUGS = frozenset({"detail"})
LIVE_BRANCH = "agnes-live"
NETWORK = "agnes-apps"
AGNES_INTERNAL_URL = "http://app:8000"

#: Anti-fork-bomb ceiling applied to every data-app container unless an
#: operator overrides ``data_apps.container_pids_limit`` in instance.yaml.
#: The runtime image's own process tree (nginx + supervisord + the app
#: process + its dependency installer) is a handful of processes; 512 is
#: generous headroom without leaving a compromised app free to fork-bomb
#: the host.
_DEFAULT_PIDS_LIMIT = 512


def _embed_credentials(url: str, username: str, password: str) -> str:
    """Insert percent-encoded basic-auth credentials after the scheme.

    Idempotent — a URL whose authority already carries credentials is returned
    unchanged. This mirrors the upstream runtime image's own
    ``embed_credentials_in_url`` (``/usr/local/keboola`` ``functions.sh``) with
    one critical difference the image's behavior forces on us: the image only
    embeds ``username``/``#password`` from ``config.json`` into **HTTPS** clone
    URLs and leaves a **plain-HTTP** URL untouched (its own bats suite asserts
    "HTTP URL - no modification"). Agnes serves the internal git backend over
    plain HTTP (``http://app:8000/data-apps.git/<slug>``), so unless we embed
    the token into the URL ourselves the container clones bare, git prompts for
    a username in a non-interactive shell, and the runtime crash-loops with
    ``could not read Username`` (proxy then 502s ``container_unreachable``).
    The image preserves a URL that already has credentials, so this is safe to
    always apply."""
    m = re.match(r"^(https?://)(.*)$", url)
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    authority = rest.split("/", 1)[0]
    if "@" in authority:  # already credentialed — don't double-embed
        return url
    return f"{scheme}{quote(username, safe='')}:{quote(password, safe='')}@{rest}"


def build_config_json(
    app_row: dict,
    *,
    secrets: dict[str, str],
    clone_url: str,
    clone_token: str,
    service_token: str,
) -> dict:
    """The runtime `config.json` the apps-runner writes for a container.

    ``clone_token`` and ``service_token`` are two different credentials and
    must not be conflated. The clone token is `data-app-git:<slug>`-scoped and
    admitted by exactly one surface — the internal git backend. The service
    token is `data-app:<slug>`-scoped and is what the running app calls the
    Agnes API with.

    They used to share one parameter. When the deploy path was corrected to
    pass the git-scoped token (so the first clone would stop failing), that
    single parameter carried it into ``AGNES_TOKEN`` as well — so every hosted
    app started successfully and was then refused by every data endpoint it
    called. The failure is invisible from the outside: the container is
    healthy, the app renders, and only its data calls 401.
    """
    if app_row["repo_mode"] == "internal":
        branch = app_row["draft_branch"] if app_row.get("is_draft") else LIVE_BRANCH
        # Embed the push token into the repository URL: the runtime image won't
        # add credentials to a plain-HTTP clone URL (see `_embed_credentials`),
        # and Agnes's internal git backend is HTTP. `username`/`#password` are
        # kept too — harmless, and they cover the HTTPS path if the internal
        # URL is ever fronted by TLS.
        git = {
            "repository": _embed_credentials(clone_url, "agnes", clone_token),
            "branch": branch,
            "username": "agnes",
            "#password": clone_token,
        }
    else:
        git = {"repository": app_row["repo_url"], "branch": app_row["repo_branch"] or "main"}
    out_secrets = {f"#{k}": v for k, v in secrets.items()}
    out_secrets["AGNES_TOKEN"] = service_token
    out_secrets["AGNES_URL"] = AGNES_INTERNAL_URL
    return {"dataApp": {"git": git, "secrets": out_secrets}}


def build_container_spec(app_row: dict, *, defaults: dict, data_dir: str) -> dict:
    slug = app_row["slug"]
    try:
        env_dict = json.loads(app_row.get("env") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"data app {slug}: invalid env JSON: {exc}") from exc
    env = {k: str(v) for k, v in env_dict.items()}
    env["AGNES_URL"] = AGNES_INTERNAL_URL
    env["AGNES_APP_ID"] = app_row["id"]
    image = defaults["runtime_image"]
    if app_row.get("runtime_tag"):
        image = image.rsplit(":", 1)[0] + ":" + app_row["runtime_tag"]
    cpu_str = app_row.get("cpu_limit") or defaults["default_cpus"]
    try:
        cpus = float(cpu_str)
    except ValueError as exc:
        raise ValueError(f"data app {slug}: invalid cpu_limit '{cpu_str}': {exc}") from exc
    return {
        "name": f"agnes-dataapp-{slug}",
        "image": image,
        "labels": {"agnes.data-app": app_row["id"]},
        "network": NETWORK,
        "config_dir": f"{data_dir}/apps/{slug}",
        "cache_volume": f"agnes-dataapp-cache-{slug}",
        "mem_limit": app_row.get("mem_limit") or defaults["default_mem_limit"],
        "cpus": cpus,
        "env": env,
        # Defense-in-depth for an internet-facing web server, mirroring the
        # posture `services/apps_runner/sandbox_api.py` already applies to
        # chat sandboxes (cap_drop/no-new-privileges/pids_limit): a data app
        # needs none of the Linux capabilities Docker grants by default,
        # gains none via a setuid binary, and a compromised one must not be
        # able to fork-bomb the host. Never applied to the chat-sandbox path
        # — the agent CLI legitimately needs broader privileges there.
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": int(defaults.get("container_pids_limit") or _DEFAULT_PIDS_LIMIT),
        # Read-only root filesystem, on by default. `container_read_only`
        # lets an operator opt out if a future runtime-image update needs a
        # rootfs write this doesn't anticipate — see
        # `docs/superpowers/specs/2026-07-21-data-apps-design.md` §10. `/data`
        # (config.json) and `/home/app/.cache` stay writable through their
        # own bind/named-volume mounts either way; `/tmp` and `/app` get a
        # tmpfs because the upstream entrypoint clones the app's repo into
        # `/app` and installs its dependencies there on every boot (§2) —
        # nothing under either path needs to survive a restart, the
        # entrypoint re-clones from scratch every time.
        "read_only": bool(defaults.get("container_read_only", True)),
        "tmpfs": {"/tmp": "", "/app": ""},
    }

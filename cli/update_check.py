"""Auto-check for a newer CLI version on the configured server.

Runs in the root typer callback before subcommand dispatch. Failure is
silent — we never block a working `agnes` command on a best-effort version
probe. Result is cached in `$AGNES_CONFIG_DIR/update_check.json` for 24h so
we don't hammer the server on every invocation.

Disable with `AGNES_NO_UPDATE_CHECK=1`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cli.config import _config_dir

_CACHE_FILENAME = "update_check.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h on a successful probe
_NEGATIVE_CACHE_TTL_SECONDS = 5 * 60  # 5min on a failed probe, to avoid
# re-probing 3s of silence (drop-packet networks: corporate firewall, VPN)
# on every `agnes` invocation.
_REQUEST_TIMEOUT_SECONDS = 3.0  # keep startup snappy


@dataclass(frozen=True)
class UpdateInfo:
    installed: str
    latest: Optional[str]
    download_url: Optional[str]

    def is_outdated(self) -> bool:
        if not self.latest or self.installed == "unknown":
            return False
        # Directional: only warn when installed < latest. `!=` would also
        # fire when the CLI is *newer* than the server (e.g. after a server
        # rollback) and prompt the user to downgrade.
        return _version_lt(self.installed, self.latest)


def _version_lt(installed: str, latest: str) -> bool:
    """Is `installed` strictly older than `latest`?

    Prefer packaging.version.Version (PEP 440, handles pre-release tags).
    Fall back to a naive dotted-int tuple for the simple N.N.N case if
    packaging is somehow unavailable. Unparseable strings return False —
    we'd rather miss an upgrade hint than prompt a silent downgrade.
    """
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(installed) < Version(latest)
        except InvalidVersion:
            pass
    except ImportError:
        pass
    try:
        a = tuple(int(x) for x in installed.split("."))
        b = tuple(int(x) for x in latest.split("."))
        return a < b
    except ValueError:
        return False


def is_disabled() -> bool:
    return os.environ.get("AGNES_NO_UPDATE_CHECK", "").lower() in ("1", "true", "yes")


def _installed_version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "unknown"


def _cache_path() -> Path:
    return _config_dir() / _CACHE_FILENAME


def _read_cache() -> Optional[dict]:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(entry: dict) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass  # best-effort — cache failure must not break the flow


def _fetch_latest(server_url: str) -> Optional[dict]:
    """Hit /cli/latest with a short timeout. Returns None on any failure."""
    import httpx
    try:
        with httpx.Client(base_url=server_url, timeout=_REQUEST_TIMEOUT_SECONDS) as c:
            resp = c.get("/cli/latest")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def check(
    server_url: Optional[str], *, bypass_disabled: bool = False
) -> Optional[UpdateInfo]:
    """Return UpdateInfo if a check ran (cached or fresh), else None.

    Silent on every failure path: no server configured, CLI package not
    installed, network down, malformed response, cache unreadable.

    `bypass_disabled=True` ignores `AGNES_NO_UPDATE_CHECK`. The env var
    silences the implicit warning loop in the root callback; an explicit
    user-typed `agnes self-upgrade` is not the implicit loop and must
    still probe. Default keeps existing call sites (root callback) silent
    when the env var is set.
    """
    if not bypass_disabled and is_disabled():
        return None
    if not server_url:
        return None

    installed = _installed_version()
    if installed == "unknown":
        return None  # can't compare without a known local version

    cache = _read_cache()
    now = time.time()
    if (
        cache
        and cache.get("installed") == installed
        and cache.get("server_url") == server_url
        and isinstance(cache.get("checked_at"), (int, float))
    ):
        age = now - cache["checked_at"]
        cached_latest = cache.get("latest")
        # Positive cache — keep for 24h. Negative cache (failed probe,
        # latest=None) — keep for 5min so we don't re-probe the 3s
        # timeout on every command when the server is silently dropping.
        ttl = _CACHE_TTL_SECONDS if cached_latest else _NEGATIVE_CACHE_TTL_SECONDS
        if age < ttl:
            if cached_latest is None:
                return None
            return UpdateInfo(
                installed=installed,
                latest=cached_latest,
                download_url=cache.get("download_url"),
            )

    payload = _fetch_latest(server_url)
    if not payload:
        # Negative cache — avoid re-probing on every invocation.
        _write_cache({
            "installed": installed,
            "server_url": server_url,
            "latest": None,
            "download_url": None,
            "checked_at": now,
        })
        return None

    latest = payload.get("version")
    dl = payload.get("download_url_path")
    download_url = f"{server_url.rstrip('/')}{dl}" if dl else None

    _write_cache({
        "installed": installed,
        "server_url": server_url,
        "latest": latest,
        "download_url": download_url,
        "checked_at": now,
    })
    return UpdateInfo(installed=installed, latest=latest, download_url=download_url)


def format_outdated_notice(info: UpdateInfo) -> str:
    """One-line stderr warning when the CLI is out of date.

    Always recommends `agnes self-upgrade` as the upgrade action. We used to
    emit `uv tool install --force {info.download_url}` with the version-pinned
    `/cli/wheel/{name}` URL the server returned — but that URL serves only the
    *current* wheel, so after a server upgrade the cached pinned URL 404s
    (issue #521). `agnes self-upgrade` re-probes `/cli/latest`, installs the
    real current wheel, smoke-tests it, and rolls back on failure — it never
    404s and always converges to the true latest, even if this banner's version
    number lags by up to the cache TTL.

    `download_url` is intentionally NOT referenced here (it stays on
    `UpdateInfo` because `agnes self-upgrade` still uses it as the install
    mechanism) — so the banner can never leak a pinned URL or the literal
    string "None".
    """
    return (
        f"[update] agnes {info.installed} is out of date — latest on this "
        f"server is {info.latest}. Upgrade: run `agnes self-upgrade`."
    )

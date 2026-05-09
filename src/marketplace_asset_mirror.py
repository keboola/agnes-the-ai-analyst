"""External-asset mirror cache for curated marketplaces.

The curator's ``.claude-plugin/agnes-metadata.json`` may reference cover
photos and doc files by external HTTP(S) URL. Linkrot would then mean the
Agnes web UI starts showing broken images / dead links the moment the
upstream CDN serves a 404. This module mirrors those URLs to disk at sync
time and serves the local copy thereafter.

**On-disk layout** (per marketplace slug)::

    ${DATA_DIR}/marketplace-cache/<slug>/
    ├── manifest.json                       # url → cache entry
    └── <plugin>/
        ├── cover.<ext>
        └── docs/<sha8>-<filename>

**Re-fetch logic per URL on every sync:**

1. URL not yet in manifest → unconditional GET, save body + record
   ETag / Last-Modified / sha256.
2. URL already mirrored → conditional GET (``If-None-Match`` /
   ``If-Modified-Since``):
   - 304 Not Modified → keep cached file, refresh ``fetched_at`` only.
   - 200 OK with same sha256 → keep file, refresh validators.
   - 200 OK with new sha256 → overwrite local file.
3. URL removed from agnes-metadata.json → ``cleanup_unused`` removes the
   manifest entry and the local file.

**Failure modes** (b1 fallback per the design discussion):
fetch failure (timeout, 4xx/5xx, allowlist reject, oversized, SSRF block)
keeps the **last good copy** intact in the cache, sets ``status = "failed_*"``
on the manifest entry, and logs a warning. The caller surfaces "mirror failed"
in the admin UI but never breaks the served plugin detail.

**SSRF guards:** only ``http(s)://`` schemes accepted, DNS resolution rejects
private / loopback / link-local / metadata IPs, 30-second timeout, 10 MB cap,
max 4 concurrent fetches per sync.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import logging
import re
import shutil
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.marketplace_assets import (
    DOC_EXTENSIONS,
    IMAGE_EXTENSIONS,
    accept_doc_response,
    accept_image_response,
    validate_doc_file,
    validate_image_file,
)

logger = logging.getLogger(__name__)

# Hardcoded operational caps. The plan deferred making these configurable —
# the comment in `instance.yaml` would be one line if/when an operator hits
# a real limit (today nothing in our org has cover images > 10 MB).
HTTP_TIMEOUT_SEC = 60
"""Per-request timeout for outgoing mirror fetches.

Larger PDFs from slow CDNs (e.g. Adobe support, government archives)
routinely exceed 30s on a residential connection — bumped from 30 → 60.
The sync runs nightly under a thread pool with bounded concurrency so
worst-case sync time grows linearly, not multiplicatively, with this
value. Operators can still cap a runaway curator by trimming
``MAX_BODY_BYTES`` (10 MB) — the timeout only matters for slow tails."""
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_CONCURRENT_FETCHES = 4

USER_AGENT = (
    "Agnes-Marketplace-Mirror/1.0 "
    "(+https://github.com/keboola/agnes-the-ai-analyst; agnes-mirror)"
)
"""HTTP User-Agent for outgoing mirror fetches.

Wikipedia / Wikimedia commons strictly enforces a User-Agent policy and
returns HTTP 400 to clients with generic strings (see
https://meta.wikimedia.org/wiki/User-Agent_policy). The format below
includes a contact URL + descriptor which satisfies their parser. Other
strict CDNs (e.g. arXiv, some news sites) similarly require a non-trivial
UA — using the same string everywhere keeps debugging simple."""

MANIFEST_FILENAME = "manifest.json"


@dataclass
class MirrorEntry:
    """One row in ``manifest.json`` — keyed by external URL."""
    url: str
    kind: str              # "cover" | "doc"
    plugin_name: str
    local: str             # relative path inside the marketplace cache dir
    etag: str = ""
    last_modified: str = ""
    sha256: str = ""
    fetched_at: str = ""   # ISO timestamp of last successful body write
    last_checked_at: str = ""  # ISO timestamp of last fetch attempt
    status: str = "unknown"   # "ok" | "failed_recent" | "failed_first" | "rejected"
    error: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "MirrorEntry":
        return cls(
            url=d.get("url", ""),
            kind=d.get("kind", ""),
            plugin_name=d.get("plugin_name", ""),
            local=d.get("local", ""),
            etag=d.get("etag", ""),
            last_modified=d.get("last_modified", ""),
            sha256=d.get("sha256", ""),
            fetched_at=d.get("fetched_at", ""),
            last_checked_at=d.get("last_checked_at", ""),
            status=d.get("status", "unknown"),
            error=d.get("error", ""),
        )


@dataclass
class MirrorReport:
    """Per-sync summary returned to the caller."""
    requested: int = 0
    fetched: int = 0
    not_modified: int = 0
    failed: int = 0
    rejected: int = 0
    removed: int = 0
    entries: Dict[str, MirrorEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SSRF / safety helpers
# ---------------------------------------------------------------------------


def _resolve_safe(url: str) -> Tuple[bool, str, str]:
    """Reject URLs we shouldn't follow and return the IP the caller MUST connect to.

    Returns ``(ok, reason, pinned_ip)``. On rejection ``pinned_ip`` is empty.

    Why the pinned IP matters: ``urllib`` would otherwise re-resolve the
    hostname at connection time, and an attacker-controlled DNS server can
    return a public IP for the validation lookup and ``127.0.0.1`` /
    ``169.254.169.254`` for the connection lookup (DNS rebinding). Resolving
    once here and connecting to that exact IP defeats the rebind. ALL
    addresses returned by ``getaddrinfo`` are validated — round-robin DNS
    that mixes public + private IPs is treated as unsafe regardless of which
    one we'd have picked first.
    """
    try:
        parts = urlparse(url)
    except ValueError as e:
        return False, f"bad_url: {e}", ""
    if parts.scheme not in ("http", "https"):
        return False, f"unsupported_scheme: {parts.scheme}", ""
    host = parts.hostname or ""
    if not host:
        return False, "missing_host", ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"dns_failure: {e}", ""

    chosen_ip = ""
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparseable_address: {ip_str}", ""
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False, f"address_in_blocked_range: {ip_str}", ""
        # AWS / GCP / Azure metadata endpoints fall under is_link_local
        # (169.254.169.254) above — explicit additional check for IPv6
        # ULA + the broad metadata-style catchall would be belt-and-
        # suspenders only.
        # Prefer the first IPv4 result for connection pinning (broader CDN
        # compatibility); fall back to the first record otherwise.
        if not chosen_ip and info[0] == socket.AF_INET:
            chosen_ip = ip_str
    if not chosen_ip and infos:
        chosen_ip = infos[0][4][0]
    if not chosen_ip:
        return False, "no_address", ""
    return True, "", chosen_ip


def _is_safe_url(url: str) -> Tuple[bool, str]:
    """Backwards-compatible 2-tuple wrapper over :func:`_resolve_safe`.

    Existing tests (and any external callers that only care about the
    accept/reject decision) keep working unchanged. The pinned IP returned
    by ``_resolve_safe`` is consumed internally by the connection-pinning
    handlers below.
    """
    ok, reason, _ = _resolve_safe(url)
    return ok, reason


# ---------------------------------------------------------------------------
# SSRF-aware urllib opener
#
# Two threats against the simple "validate URL, then urlopen" pattern:
#   1. Redirect bypass — urllib follows up to 10 redirects with no SSRF
#      re-check. An attacker 302s to http://169.254.169.254/... and we
#      mirror cloud metadata.
#   2. DNS rebinding — _resolve_safe calls getaddrinfo once; urllib calls
#      it again to connect. Attacker DNS returns a public IP first, a
#      private IP second.
#
# Mitigation is one shared opener built once, with three handlers:
#   - _SafeRedirectHandler    — re-validates Location on every redirect
#   - _PinnedHTTPHandler      — connects to the SSRF-validated IP, never
#                                re-resolves the hostname
#   - _PinnedHTTPSHandler     — same, with TLS SNI/cert verification still
#                                bound to the original hostname
# ---------------------------------------------------------------------------


class _UnsafeRedirectError(Exception):
    """Raised by ``_SafeRedirectHandler`` when a redirect Location is unsafe.

    Distinct exception type so ``_fetch_url`` can map this to
    ``status='rejected'`` (terminal — security decision, not a transient
    network failure to retry next sync).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-runs SSRF validation on every redirect ``Location`` header.

    The default ``HTTPRedirectHandler`` follows up to 10 redirects without
    revalidation, defeating the pre-flight SSRF guard. Re-pinning the IP
    on the new request preserves DNS-rebinding defence across hops too.
    """

    # Tightened from the urllib default of 10. Legitimate CDN chains
    # (S3 → presigned, DOI → publisher) routinely use 3–4 hops; 5 leaves
    # headroom without giving attackers many hops to scan.
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason, pinned_ip = _resolve_safe(newurl)
        if not ok:
            raise _UnsafeRedirectError(f"redirect_blocked: {reason}")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            # Re-pin the validated IP so the next leg connects to it
            # rather than re-resolving the hostname (rebinding defence).
            new_req.pinned_ip = pinned_ip
        return new_req


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-resolved IP.

    Replicates the parent ``connect()`` but skips the implicit DNS lookup
    inside ``socket.create_connection``. The hostname stays in ``self.host``
    for the ``Host:`` header so vhost routing still works.
    """

    def __init__(self, host, *, pinned_ip, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection variant of ``_PinnedHTTPConnection``.

    TLS SNI + certificate verification stay bound to the original hostname
    (``self.host``) — the cert chain is validated against the hostname the
    curator wrote, not the pinned IP. Connecting to a different IP than
    DNS would currently return is exactly the point.
    """

    def __init__(self, host, *, pinned_ip, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        pinned = getattr(req, "pinned_ip", None)
        if not pinned:
            # Fail closed: any unpinned request reaching the handler is a
            # bug (caller forgot to use _PinnedRequest). Better to error
            # than to silently re-resolve and lose the rebinding defence.
            raise urllib.error.URLError("ssrf: missing pinned_ip on request")
        return self.do_open(
            lambda host, **kw: _PinnedHTTPConnection(host, pinned_ip=pinned, **kw),
            req,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        pinned = getattr(req, "pinned_ip", None)
        if not pinned:
            raise urllib.error.URLError("ssrf: missing pinned_ip on request")
        return self.do_open(
            lambda host, **kw: _PinnedHTTPSConnection(host, pinned_ip=pinned, **kw),
            req,
        )


class _PinnedRequest(urllib.request.Request):
    """``urllib.Request`` carrying the SSRF-validated IP for connection pinning."""

    def __init__(self, url, *, pinned_ip, **kwargs):
        super().__init__(url, **kwargs)
        self.pinned_ip = pinned_ip


_OPENER = urllib.request.build_opener(
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
    _SafeRedirectHandler(),
)


def _http_open(req, timeout):
    """Single call site for outgoing HTTP — wraps the SSRF-aware opener.

    Tests patch this symbol to inject fake responses without going through
    the urllib stack. Production code never bypasses it.
    """
    return _OPENER.open(req, timeout=timeout)


def _safe_filename(url: str, default_ext: str) -> str:
    """Derive a stable, FS-safe filename from a URL.

    Format: ``<sha8(url)>-<basename>``. The hash prefix means two URLs with
    the same trailing filename don't collide; the human-readable basename
    helps when an operator browses the cache dir directly.
    """
    parts = urlparse(url)
    base = Path(parts.path).name or "download"
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", base)[:64]
    if not base or base.startswith("."):
        base = f"download{default_ext}"
    sha8 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{sha8}-{base}"


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------


def _load_manifest(cache_dir: Path) -> Dict[str, MirrorEntry]:
    path = cache_dir / MANIFEST_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("mirror manifest %s unreadable, starting fresh: %s", path, e)
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    out: Dict[str, MirrorEntry] = {}
    for url, raw in entries.items():
        if isinstance(raw, dict):
            out[url] = MirrorEntry.from_json(raw)
    return out


def _write_manifest(cache_dir: Path, entries: Dict[str, MirrorEntry]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / MANIFEST_FILENAME
    body = {
        "version": 1,
        "entries": {url: e.to_json() for url, e in entries.items()},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


@dataclass
class FetchOutcome:
    status: str            # "ok" | "not_modified" | "failed" | "rejected"
    body: bytes = b""
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""
    error: str = ""


def _fetch_url(
    url: str,
    *,
    prior: Optional[MirrorEntry],
    expect_kind: str,
) -> FetchOutcome:
    """Single HTTP GET (with conditional headers when ``prior`` provides them).

    SSRF + size + allowlist enforcement happen here. Any rejection produces
    ``status="rejected"`` (terminal — caller doesn't retry); any transient
    network error produces ``status="failed"`` (caller may surface and try
    again next sync).
    """
    safe, reason, pinned_ip = _resolve_safe(url)
    if not safe:
        return FetchOutcome(status="rejected", error=reason)

    headers: Dict[str, str] = {"User-Agent": USER_AGENT}
    if prior:
        if prior.etag:
            headers["If-None-Match"] = prior.etag
        if prior.last_modified:
            headers["If-Modified-Since"] = prior.last_modified

    req = _PinnedRequest(url, pinned_ip=pinned_ip, headers=headers)
    try:
        with _http_open(req, HTTP_TIMEOUT_SEC) as resp:
            status_code = resp.status
            content_type = resp.headers.get("Content-Type", "") or ""
            etag = resp.headers.get("ETag", "") or ""
            last_modified = resp.headers.get("Last-Modified", "") or ""
            # Allowlist gate based on Content-Type (cheaper than reading body
            # before deciding). For docs we additionally accept generic types
            # backed by a URL-extension match.
            if expect_kind == "cover":
                check = accept_image_response(url, content_type)
            else:
                check = accept_doc_response(url, content_type)
            if not check.ok:
                return FetchOutcome(
                    status="rejected",
                    content_type=content_type,
                    error=check.reason,
                )
            # Read with a hard cap so a misbehaving server can't OOM us.
            body = resp.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                return FetchOutcome(
                    status="rejected",
                    error=f"body_exceeds_cap: > {MAX_BODY_BYTES} bytes",
                )
            return FetchOutcome(
                status="ok",
                body=body,
                content_type=content_type,
                etag=etag,
                last_modified=last_modified,
            )
    except _UnsafeRedirectError as e:
        return FetchOutcome(status="rejected", error=e.reason)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return FetchOutcome(
                status="not_modified",
                etag=prior.etag if prior else "",
                last_modified=prior.last_modified if prior else "",
            )
        return FetchOutcome(status="failed", error=f"http_{e.code}")
    except urllib.error.URLError as e:
        # urllib wraps non-HTTPError exceptions raised inside handlers in
        # URLError; unwrap so a redirect-blocked error doesn't get
        # mis-classified as a transient failure.
        if isinstance(getattr(e, "reason", None), _UnsafeRedirectError):
            return FetchOutcome(status="rejected", error=e.reason.reason)
        return FetchOutcome(status="failed", error=f"url_error: {e.reason}")
    except (TimeoutError, socket.timeout):
        return FetchOutcome(status="failed", error="timeout")
    except Exception as e:  # noqa: BLE001 — defensive, never abort the sync
        logger.exception("mirror fetch crashed for %s", url)
        return FetchOutcome(status="failed", error=f"crash: {e!r}")


# ---------------------------------------------------------------------------
# Body-side validation + write
# ---------------------------------------------------------------------------


def _validate_body(filename: str, body: bytes, kind: str):
    if kind == "cover":
        return validate_image_file(filename, body)
    return validate_doc_file(filename, body)


def _local_relpath(plugin_name: str, kind: str, fname: str) -> str:
    if kind == "cover":
        return f"{plugin_name}/{fname}"
    return f"{plugin_name}/docs/{fname}"


def _write_body(cache_dir: Path, relpath: str, body: bytes) -> None:
    """Write ``body`` to ``cache_dir/relpath`` atomically (tmp + rename)."""
    full = cache_dir / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp = full.with_suffix(full.suffix + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(full)


# ---------------------------------------------------------------------------
# Public API — one entry point per plugin
# ---------------------------------------------------------------------------


def sync_assets(
    *,
    cache_dir: Path,
    requests: List[Tuple[str, str, str]],
) -> MirrorReport:
    """Mirror every URL in ``requests`` into ``cache_dir``.

    ``requests`` is a list of ``(plugin_name, kind, url)`` tuples produced by
    :func:`src.marketplace_metadata.collect_external_urls`. Returns a
    :class:`MirrorReport` summarising the outcome plus the resulting manifest
    so the caller can build a ``url → served_path`` lookup for the DB write.

    Exceptions inside :func:`_fetch_url` and :func:`_write_body` are caught
    by the surrounding ``except`` so one bad URL never aborts the rest of the
    sync. URLs absent from ``requests`` but present in the existing manifest
    are removed from disk + manifest (the curator dropped them upstream).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(cache_dir)
    report = MirrorReport(requested=len(requests))
    requested_urls = {url for _, _, url in requests}

    # Phase 1 — fetch every requested URL with bounded concurrency.
    def _do_one(req: Tuple[str, str, str]) -> Tuple[Tuple[str, str, str], FetchOutcome]:
        plugin_name, kind, url = req
        prior = manifest.get(url)
        outcome = _fetch_url(url, prior=prior, expect_kind=kind)
        return req, outcome

    results: List[Tuple[Tuple[str, str, str], FetchOutcome]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_FETCHES
    ) as pool:
        for result in pool.map(_do_one, requests):
            results.append(result)

    # Phase 2 — process outcomes serially so manifest writes are deterministic.
    now_iso = datetime.now(timezone.utc).isoformat()
    for (plugin_name, kind, url), outcome in results:
        prior = manifest.get(url)
        if outcome.status == "not_modified" and prior:
            prior.last_checked_at = now_iso
            prior.error = ""
            prior.status = "ok"  # 304 means the cached file is still valid
            manifest[url] = prior
            report.not_modified += 1
            continue
        if outcome.status == "rejected":
            entry = prior or MirrorEntry(url=url, kind=kind, plugin_name=plugin_name, local="")
            entry.status = "rejected"
            entry.last_checked_at = now_iso
            entry.error = outcome.error
            manifest[url] = entry
            report.rejected += 1
            logger.warning(
                "mirror rejected url=%s kind=%s reason=%s",
                url, kind, outcome.error,
            )
            continue
        if outcome.status == "failed":
            entry = prior or MirrorEntry(url=url, kind=kind, plugin_name=plugin_name, local="")
            # First-time failures distinguish from "we previously had a copy" failures
            entry.status = "failed_recent" if prior and prior.local else "failed_first"
            entry.last_checked_at = now_iso
            entry.error = outcome.error
            manifest[url] = entry
            report.failed += 1
            logger.warning(
                "mirror fetch failed url=%s kind=%s reason=%s (keep_prior=%s)",
                url, kind, outcome.error, bool(prior and prior.local),
            )
            continue
        # outcome.status == "ok" — body present
        # Pick filename: extension comes from URL preferentially, else from
        # Content-Type. Fall back to a kind-default when neither is helpful.
        default_ext = ".bin"
        if kind == "cover":
            for e in IMAGE_EXTENSIONS:
                if url.lower().endswith(e):
                    default_ext = e
                    break
            else:
                ct = outcome.content_type.split(";", 1)[0].strip().lower()
                default_ext = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                }.get(ct, ".png")
        else:
            for e in DOC_EXTENSIONS:
                if url.lower().endswith(e):
                    default_ext = e
                    break
            else:
                ct = outcome.content_type.split(";", 1)[0].strip().lower()
                default_ext = {
                    "application/pdf": ".pdf",
                    "text/markdown": ".md",
                    "text/x-markdown": ".md",
                    "text/plain": ".txt",
                }.get(ct, ".txt")
        fname = _safe_filename(url, default_ext)
        # Ensure the chosen filename has the resolved extension so body
        # validation (which looks at the suffix) accepts it.
        if not fname.lower().endswith(default_ext):
            fname = fname + default_ext
        validation = _validate_body(fname, outcome.body, kind)
        if not validation.ok:
            entry = prior or MirrorEntry(url=url, kind=kind, plugin_name=plugin_name, local="")
            entry.status = "rejected"
            entry.last_checked_at = now_iso
            entry.error = f"body_validation: {validation.reason}"
            manifest[url] = entry
            report.rejected += 1
            logger.warning(
                "mirror body rejected url=%s reason=%s",
                url, validation.reason,
            )
            continue
        new_sha = hashlib.sha256(outcome.body).hexdigest()
        relpath = _local_relpath(plugin_name, kind, fname)
        # Only rewrite the body when the hash actually changed — saves disk
        # IO + lets future-us correlate "fetched_at" with content changes.
        if not prior or prior.sha256 != new_sha or not (cache_dir / prior.local).is_file():
            try:
                _write_body(cache_dir, relpath, outcome.body)
            except OSError as e:
                logger.warning("mirror write failed url=%s: %s", url, e)
                report.failed += 1
                continue
            entry = MirrorEntry(
                url=url,
                kind=kind,
                plugin_name=plugin_name,
                local=relpath,
                etag=outcome.etag,
                last_modified=outcome.last_modified,
                sha256=new_sha,
                fetched_at=now_iso,
                last_checked_at=now_iso,
                status="ok",
            )
            manifest[url] = entry
            # Persist the manifest BEFORE unlinking the old body. A kill -9
            # between body-write and the end-of-batch persist would otherwise
            # leave on-disk files the next sync's manifest never references —
            # disk bloats over time as URLs come and go from the curator's
            # agnes-metadata.json. Per-iteration persist narrows the crash
            # window from "all of Phase 2" to "between persist and unlink"
            # (microseconds). Cost: ~one tmp+rename per body write; manifest
            # is a few KB so the overhead is negligible vs. the HTTP fetches.
            try:
                _write_manifest(cache_dir, manifest)
            except OSError as e:
                # Body is on disk but the manifest didn't commit. Don't
                # unlink the old body — the on-disk manifest still
                # references it, and serving a stale-but-existing file
                # beats serving a 404.
                logger.warning(
                    "mirror manifest persist failed mid-batch url=%s: %s", url, e,
                )
                report.failed += 1
                continue
            # If the previous local file lived at a different path, drop it.
            if prior and prior.local and prior.local != relpath:
                try:
                    (cache_dir / prior.local).unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            prior.etag = outcome.etag or prior.etag
            prior.last_modified = outcome.last_modified or prior.last_modified
            prior.last_checked_at = now_iso
            prior.status = "ok"
            prior.error = ""
            manifest[url] = prior
        report.fetched += 1

    # Phase 3 — drop manifest entries the curator removed upstream, plus
    # their on-disk bodies. Same persist-before-unlink discipline as
    # Phase 2: collect the relpaths to delete, persist the manifest with
    # the entries already gone, *then* unlink. A crash mid-cleanup leaves
    # at most a microsecond window where a file is still on disk despite
    # the manifest no longer naming it — the next sync simply re-reads
    # the (now-correct) manifest and the orphan stays orphaned, but the
    # served state stays consistent.
    removed_paths: List[str] = []
    for url in list(manifest.keys()):
        if url in requested_urls:
            continue
        entry = manifest.pop(url)
        if entry.local:
            removed_paths.append(entry.local)
        report.removed += 1

    _write_manifest(cache_dir, manifest)

    for relpath in removed_paths:
        try:
            (cache_dir / relpath).unlink(missing_ok=True)
        except OSError:
            pass

    report.entries = manifest
    return report


def delete_cache_dir(cache_dir: Path) -> bool:
    """Remove the entire mirror cache for one marketplace. True iff removed."""
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
        return True
    return False

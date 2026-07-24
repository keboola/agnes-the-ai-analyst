"""SSRF-hardened, HMAC-signed outbound webhook delivery (V1b Task 6,
`docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md`).

This is the highest-risk security surface in the agent-profiles feature set:
an owner registers an arbitrary HTTPS URL, and this server's own network
identity then makes an outbound request to it. Two hardening decisions
carry the whole module:

**C10 — resolve-and-PIN, not just re-validate (SSRF / DNS rebinding).**
`httpx` (like any HTTP client) re-resolves the hostname at connect time by
default — validating a URL and then handing the *hostname* to the HTTP
client leaves a TOCTOU window: an attacker-controlled DNS name can resolve
to a public IP at validation time and to a cloud-metadata or internal
address (``169.254.169.254``, ``10.0.0.0/8``, ...) a few milliseconds later
at connect time ("DNS rebinding"). `validate_and_resolve` closes this by
resolving the hostname to a concrete IP itself (once, via
`socket.getaddrinfo`), verifying THAT IP is public, and returning it so the
caller can connect directly to the pinned IP — never handing the hostname
to the HTTP client's own resolver. `deliver` re-runs the full
resolve-and-pin (not merely a re-validate) immediately before every send,
so a URL that pointed somewhere safe at registration time but has since
been repointed at an internal address is caught on every delivery attempt,
not just the first. The TLS handshake still authenticates against the
*original* hostname (via httpx/httpcore's `sni_hostname` request
extension), so certificate validation is unaffected by connecting to a raw
IP.

**C11 — the payload is a NOTIFICATION, not the answer (egress privacy).**
A webhook fires on `job.completed`/`job.failed` for an `agent_response`
background job. The POST body deliberately carries ONLY
`{event, job_id, agent_slug, status, ts}` — never the agent's answer or any
other data the turn touched. Silently exfiltrating a user's data to an
owner-supplied external URL would be a much larger privacy hole than SSRF
alone; the receiver is expected to fetch the actual result afterward via
`GET /api/v1/jobs/{job_id}` (owner/agent-PAT authenticated), the same read
path a synchronous caller already uses.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)

#: `jobs.kind` this module's deliveries are enqueued under — see
#: `app/worker/kinds.py::_run_webhook_deliver`.
WEBHOOK_DELIVER_KIND = "webhook-deliver"

#: `agent_webhooks.events` value -> internal job status. Anything else
#: (an unrecognized status) is a silent no-op — see `enqueue_job_event_webhooks`.
_EVENT_FOR_STATUS = {"completed": "job.completed", "failed": "job.failed"}

#: The cloud-metadata endpoint every major provider (AWS/GCP/Azure) serves
#: unauthenticated instance-credential data from. Explicitly denied even
#: though it also falls under `is_link_local` below — belt and suspenders,
#: and it documents the specific threat this guard exists for.
_METADATA_IPV4 = ipaddress.ip_address("169.254.169.254")

#: IPv6 Unique Local Addresses (RFC 4193) — the IPv6 analogue of RFC1918
#: private ranges. `fd00::/8` is the documented-example half of the
#: `fc00::/7` ULA block; checked explicitly even though `is_private` already
#: covers the full `fc00::/7` range.
_ULA_NET = ipaddress.ip_network("fd00::/8")

#: Carrier-Grade NAT / Shared Address Space (RFC 6598) — NOT globally
#: routable, used for internal ISP/telco and (notably) some cloud-provider
#: internal networking. Checked explicitly because Python's `ipaddress`
#: does NOT classify this range as `is_private`/`is_reserved` (verified
#: against the stdlib directly) even though it is exactly the kind of
#: internal-only space this guard exists to keep unreachable.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

#: 6to4 (RFC 3056) — encodes an arbitrary IPv4 address in bits 16-47 of a
#: `2002::/16` address. Python's `ipaddress` reports these as `is_global`
#: (the `2002::/16` prefix itself isn't private), so a 6to4-encoded private
#: target (e.g. `2002:0a00:0001::` embedding `10.0.0.1`) would otherwise
#: slip past every other check here. Denying the whole block is simpler and
#: safer than decoding the embedded address and re-checking it.
_SIXTOFOUR_NET = ipaddress.ip_network("2002::/16")

_DEFAULT_DELIVERY_TIMEOUT_S = 10.0
_DEFAULT_MAX_FAILURES = 5

#: Cap on how much of a webhook response body we bother reading — enough to
#: notice a malformed/oversized response, small enough that a slow or
#: malicious endpoint streaming megabytes can't pin a worker slot buffering
#: it. Only the status code is ever used; the body is discarded either way.
_MAX_RESPONSE_BYTES_READ = 8 * 1024


def _is_forbidden_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True if ``ip`` must never be connected to on this server's behalf —
    private/loopback/link-local/reserved/multicast/unspecified ranges, the
    cloud metadata address, IPv6 ULA space, 6to4 space, and CGNAT space. An
    IPv4-mapped IPv6 address (``::ffff:10.0.0.1``) is unwrapped first so it
    can't smuggle a forbidden IPv4 target past an IPv6-only check.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
        elif ip in _ULA_NET or ip in _SIXTOFOUR_NET:
            return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip == _METADATA_IPV4
    )


def validate_and_resolve(url: str) -> str:
    """Validate a webhook URL and return the single public IP it should be
    connected to (see the module docstring's C10 for why this returns a
    pinned IP rather than just raising/passing on success).

    Requirements, each raising ``ValueError`` on failure:

    - scheme must be ``https`` (plain ``http`` is always rejected — a
      webhook secret/signature travelling in cleartext is a separate
      exposure this guard also closes);
    - the host must resolve (``socket.getaddrinfo``) — any resolution
      error (typo, NXDOMAIN, transient DNS outage) is denied, not retried;
    - EVERY resolved address must be public — if any single result (a
      hostname can resolve to several) is
      private/loopback/link-local/reserved/multicast/unspecified/metadata/ULA,
      the whole URL is denied. This is deliberately stricter than "at least
      one address is public": an attacker who controls DNS can return a
      public decoy alongside a private target and let round-robin/failover
      logic pick either one.

    Returns the first resolved address (all were already verified public)
    for the caller to connect to directly.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"webhook url must use https, got scheme {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("webhook url must include a host")

    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve webhook host {host!r}: {exc}") from exc
    if not addrinfo:
        raise ValueError(f"webhook host {host!r} resolved to no addresses")

    resolved: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise ValueError(f"webhook host {host!r} resolved to an unparseable address {ip_str!r}") from exc
        if _is_forbidden_ip(ip):
            raise ValueError(
                f"webhook host {host!r} resolves to a forbidden address ({ip_str}) — private, "
                "loopback, link-local, reserved, and cloud-metadata ranges are not reachable"
            )
        resolved.append(ip_str)

    return resolved[0]


def sign(secret: str, body: bytes) -> str:
    """``"sha256=" + HMAC-SHA256(secret, body)`` (hex digest) — the value
    sent in the ``x-agnes-signature`` header on every delivery. Receivers
    verify by recomputing this over the raw request body with the secret
    they were shown once at webhook creation."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _delivery_timeout_s() -> float:
    raw = os.environ.get("AGNES_WEBHOOK_DELIVERY_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_DELIVERY_TIMEOUT_S
    try:
        return max(float(raw), 0.1)
    except ValueError:
        return _DEFAULT_DELIVERY_TIMEOUT_S


def _load_chat_config() -> Any:
    """Fresh-load `ChatConfig` from the same overlay path the request-serving
    process reads at CHAT-INIT (`app/main.py`) — deferred import + fresh
    read (not cached) since this module runs from a worker THREAD with no
    `Request`/`app.state` to read the already-loaded config off of (see
    `app/worker/kinds.py`'s module docstring for the same deferred-import
    rationale)."""
    from app.chat.config import load_chat_config
    from app.secrets import _state_dir

    return load_chat_config(_state_dir() / "instance.yaml")


def webhook_max_failures() -> int:
    """Consecutive-failure threshold past which `deliver` disables a webhook
    (`agent_api.webhook_max_failures` in `instance.yaml`'s `chat:` block,
    default 5). An env override (`AGNES_WEBHOOK_MAX_FAILURES`) takes
    precedence — same knob-layering convention as
    `app/worker/kinds.py`'s lease-second helpers."""
    raw = os.environ.get("AGNES_WEBHOOK_MAX_FAILURES")
    if raw is not None:
        try:
            return max(int(raw), 1)
        except ValueError:
            pass
    try:
        cfg = _load_chat_config()
        return max(
            int(getattr(cfg, "agent_api_webhook_max_failures", _DEFAULT_MAX_FAILURES) or _DEFAULT_MAX_FAILURES), 1
        )
    except Exception:
        logger.exception("webhook-delivery: failed to load agent_api.webhook_max_failures, using default")
        return _DEFAULT_MAX_FAILURES


def _pinned_url(original_url: str, pinned_ip: str) -> str:
    """Rebuild ``original_url`` with its host replaced by ``pinned_ip``,
    preserving scheme/port/path/query — an IPv6 literal is bracketed per
    RFC 3986. The ``Host``/SNI still carry the ORIGINAL hostname (set by the
    caller), so this is purely about which socket gets connected to."""
    parsed = urlparse(original_url)
    port = parsed.port
    host_part = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = f"{host_part}:{port}" if port is not None else host_part
    return urlunparse((parsed.scheme, netloc, parsed.path or "/", parsed.params, parsed.query, ""))


def _host_header(parsed) -> str:
    """``Host`` header value for a parsed URL — includes the port when it
    isn't the scheme's default. RFC 9110 §7.2 requires the port whenever
    it's given and non-default; every webhook URL here is ``https`` (see
    ``validate_and_resolve``), so the default is 443 and e.g.
    ``hooks.example.com:8443`` must carry the ``:8443`` explicitly or a
    receiver doing vhost/port-based routing sees the wrong target."""
    host = parsed.hostname or ""
    port = parsed.port
    if port is not None and port != 443:
        return f"{host}:{port}"
    return host


def _post_to_pinned_ip(url: str, secret: str, pinned_ip: str, payload: dict) -> bool:
    """Connect to ``pinned_ip`` directly (never the hostname — see the
    module docstring's C10) with the original ``Host`` header and TLS SNI,
    HMAC-signed body, no redirect following, short timeout. Returns whether
    the response was 2xx.

    Streams the response (``httpx.Client.stream``, not ``.post``) instead
    of eagerly buffering the whole body, and reads at most
    ``_MAX_RESPONSE_BYTES_READ`` bytes of it under an explicit TOTAL
    wall-clock deadline — not just ``httpx``'s per-chunk read timeout. A
    slow-drip endpoint that sends a trickle of bytes each just under the
    per-chunk timeout, forever, never trips any single operation's timeout
    on its own; without a cumulative deadline it would pin this worker
    slot indefinitely. Only the status code is used — the body is
    discarded either way.
    """
    parsed = urlparse(url)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {
        "Host": _host_header(parsed),
        "Content-Type": "application/json",
        "x-agnes-signature": sign(secret, body),
    }
    target_url = _pinned_url(url, pinned_ip)
    timeout_s = _delivery_timeout_s()
    deadline = time.monotonic() + timeout_s
    # `trust_env=False`: by default httpx/httpcore reads proxy config
    # (`HTTPS_PROXY`/`NO_PROXY`/...) from the environment. When a proxy IS
    # configured, httpcore's CONNECT-tunnel path hardcodes the TLS
    # `server_hostname` it validates against to the pinned IP we connect
    # to and ignores the `sni_hostname` extension set below — silently
    # breaking certificate validation (wrong SNI) on every delivery from a
    # proxied deployment. This module always connects to an explicitly
    # resolved/pinned IP itself (module docstring C10) and has no
    # legitimate use for an ambient proxy, so disable it outright.
    with httpx.Client(timeout=timeout_s, trust_env=False) as client:
        with client.stream(
            "POST",
            target_url,
            content=body,
            headers=headers,
            # Forces TLS certificate/SNI validation against the ORIGINAL
            # hostname even though the socket connects to `pinned_ip` — see
            # module docstring. Supported by httpx's httpcore transport.
            extensions={"sni_hostname": parsed.hostname},
            follow_redirects=False,
        ) as resp:
            ok = 200 <= resp.status_code < 300
            read = 0
            for chunk in resp.iter_bytes():
                read += len(chunk)
                if read >= _MAX_RESPONSE_BYTES_READ:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"webhook response body exceeded the {timeout_s:.1f}s delivery deadline")
    return ok


def deliver(webhook_row: dict, payload: dict) -> bool:
    """Deliver one HMAC-signed notification POST to ``webhook_row``.

    Re-runs `validate_and_resolve` immediately before sending (DNS-rebind
    defense — see module docstring C10) and connects to the freshly pinned
    IP, never the hostname. ``payload`` must already be the NOTIFICATION
    shape (C11) — this function does not enforce that itself, that's
    `enqueue_job_event_webhooks`'s job; `deliver` only signs and sends
    whatever dict it's given.

    On success, records it (`agent_webhooks_repo().record_success`, which
    also resets the consecutive-failure counter). On failure — SSRF
    re-validation denial, connection error, timeout, or non-2xx response —
    records the failure and, once `webhook_max_failures()` consecutive
    failures accumulate, disables the webhook
    (`agent_webhooks_repo().disable`) so a permanently-broken or
    now-malicious endpoint stops being retried indefinitely.

    Returns whether the delivery succeeded (2xx response).
    """
    from src.repositories import agent_webhooks_repo

    ok = False
    try:
        pinned_ip = validate_and_resolve(webhook_row["url"])
    except ValueError:
        logger.warning(
            "webhook %s: SSRF re-validation denied delivery at send time (url may now resolve to a forbidden address)",
            webhook_row.get("id"),
        )
    else:
        try:
            ok = _post_to_pinned_ip(webhook_row["url"], webhook_row["secret"], pinned_ip, payload)
        except Exception:
            logger.warning("webhook %s: delivery request failed", webhook_row.get("id"), exc_info=True)
            ok = False

    repo = agent_webhooks_repo()
    if ok:
        repo.record_success(webhook_row["id"])
    else:
        # `record_failure` returns `0` if the webhook was deleted between
        # this job's claim and this call landing — that's below any
        # configured `webhook_max_failures()` (always >= 1), so the `>=`
        # check below is naturally a no-op ("webhook vanished, stop")
        # rather than trying to `disable()` a row that no longer exists.
        failures = repo.record_failure(webhook_row["id"])
        if failures >= webhook_max_failures():
            repo.disable(webhook_row["id"])
            logger.warning(
                "webhook %s: disabled after %d consecutive delivery failures", webhook_row.get("id"), failures
            )
    return ok


def enqueue_job_event_webhooks(*, agent_id: str, job_id: str, status: str) -> None:
    """Fan out a `job.completed`/`job.failed` notification to every active
    webhook registered for ``agent_id`` on that event — enqueuing one
    `webhook-deliver` job per webhook (`app/worker/kinds.py`'s
    `_run_webhook_deliver` handler calls `deliver` for each).

    ``status`` must be ``"completed"`` or ``"failed"``; anything else is a
    silent no-op (defensive — callers pass this through from job-status
    bookkeeping, which is a closed vocabulary).

    Privacy (C11, see module docstring): the notification body built here
    is EXACTLY `{event, job_id, agent_slug, status, ts}` — never the agent's
    answer, prompt, or any other job payload/result data.

    Best-effort at every step (list lookup, agent-slug resolution, each
    individual enqueue) — this is called from a job's own terminal-state
    transition (`app/worker/runtime.py`), and a webhook-notification hiccup
    must never be able to un-finalize (retry/fail) a job that already
    completed or failed.
    """
    event = _EVENT_FOR_STATUS.get(status)
    if event is None:
        return

    from src.repositories import agent_webhooks_repo, agents_repo, jobs_repo

    try:
        webhooks = agent_webhooks_repo().list_active_for_event(agent_id, event)
    except Exception:
        logger.exception("webhook notify: list_active_for_event failed for agent=%s event=%s", agent_id, event)
        return
    if not webhooks:
        return

    agent_slug: Optional[str] = None
    try:
        agent = agents_repo().get_by_id(agent_id)
        agent_slug = agent.get("slug") if agent else None
    except Exception:
        logger.exception("webhook notify: agents_repo().get_by_id failed for agent=%s", agent_id)

    notification = {
        "event": event,
        "job_id": job_id,
        "agent_slug": agent_slug,
        "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    jobs = jobs_repo()
    for webhook in webhooks:
        try:
            jobs.enqueue(WEBHOOK_DELIVER_KIND, {"webhook_id": webhook["id"], "notification": notification})
        except Exception:
            logger.exception("webhook notify: failed to enqueue delivery for webhook=%s", webhook.get("id"))

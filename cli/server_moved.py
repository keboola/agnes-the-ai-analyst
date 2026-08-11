"""One redirect diagnosis, shared by every CLI HTTP client.

A deployment that changes hostname leaves the old name answering `308` for
a while. Neither CLI client follows redirects, and that is deliberate:
httpx strips `Authorization` on a cross-origin hop
(``httpx._client.Client._redirect_headers``), so following one would land
unauthenticated — the same failure under a more confusing name, and a
credential sent to a host the user never configured.

So a 3xx has to become an explanation instead. This module owns that
explanation because the CLI has TWO HTTP clients and the first attempt
(#1225) taught only one of them: ``cli/client.py`` runs an event hook,
while ``cli/v2_client.py`` calls module-level ``httpx.get`` / ``httpx.post``
and raises only on ``>= 400`` — so a redirect fell through to ``r.json()``
on a redirect's empty body and ten command modules answered

    Error: internal CLI error (JSONDecodeError).

Anything new that talks HTTP should call ``moved_server_message`` rather
than re-deriving this, and ``tests/test_v2client_moved_server.py`` fails a
client that makes httpx calls without consulting it.
"""

from __future__ import annotations

from typing import Optional

import httpx

#: Redirect statuses an API call can come back with. All of them mean the
#: same thing here: the request never reached a handler.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def is_redirect(status_code: int) -> bool:
    return status_code in REDIRECT_STATUSES


def _parse_target(location: str, configured: str) -> tuple[bool, str]:
    """``(is_cross_host_move, new_base_url)`` for a ``Location`` header.

    One parser for both the prose message and the typed body, so the two can
    never disagree about whether a redirect is a move.

    ``location`` may be absolute, protocol-relative (``//host/path`` —
    absolute despite the leading slash), or relative.
    """
    configured = (configured or "").rstrip("/")
    moved = False
    new_base = ""
    if location:
        try:
            target = httpx.URL(location)
            if not target.scheme:
                # Protocol-relative (`//host/path`): inherit the scheme we
                # dialed with, so the hint is a URL the user can paste rather
                # than `//host`. Must happen before the host test — the scheme
                # is what makes the rendered `new_base` usable.
                target = target.copy_with(scheme=httpx.URL(configured).scheme or "https")
            # A move is "the target names a DIFFERENT host", decided on the
            # parsed URL rather than on how the header is spelled. `Location`
            # may be any URI-reference, so a path-relative `v2/agents` has no
            # host at all — a textual "does not start with /" test read that
            # as absolute and derived a hostless `AGNES_SERVER=https://`,
            # telling the user to point at an address that cannot exist.
            # No host means same origin, which falls through to the generic
            # message below.
            moved = bool(target.netloc) and target.netloc != httpx.URL(configured).netloc
            if moved:
                new_base = str(target.copy_with(raw_path=b"/")).rstrip("/")
        except Exception:
            moved = False
    return moved, new_base


def moved_server_message(status_code: int, location: str, configured: str) -> str:
    """Explain a redirect in prose, for the client that writes to stderr.

    Only a genuine cross-host move gets the re-point instructions; telling
    someone to change a hostname they are already using is noise.
    """
    configured = (configured or "").rstrip("/")
    moved, new_base = _parse_target(location, configured)

    where = f" (redirect to {location})" if location else " (redirect)"
    head = f"{configured} answered HTTP {status_code}{where} instead of handling the request."

    if moved and new_base:
        return "\n".join(
            [
                head,
                "That address has moved. Redirects are not followed automatically:",
                "your credentials are stripped on a cross-origin hop, so the retry",
                "would fail as 'not authenticated' rather than work.",
                "Point the CLI at the new address:",
                f"  AGNES_SERVER={new_base} agnes <command>     (one-off)",
                f"  or set `server: {new_base}` in your agnes config.yaml",
            ]
        )
    return (
        f"{head}\nThe CLI does not follow redirects on API calls. If this is "
        "unexpected, check whether a proxy sits in front of the server."
    )


def redirect_target(location: str, configured: str) -> str:
    """The moved-to base URL, or ``""`` when this is not a cross-host move."""
    _, new_base = _parse_target(location, configured)
    return new_base


def redirect_body(response: "httpx.Response", configured: str) -> dict:
    """A typed error body for a redirect, for clients that raise rather than exit.

    The remedy is split across ``fix`` / ``config`` rather than folded into
    ``hint``, because ``cli.error_render`` WRAPS ``hint`` at 80 columns: the
    whole explanation in one field reflowed into a paragraph and split the
    new hostname mid-token —

        or set `server: https://agnes-analytics-
        platform.example.com` in your agnes config.yaml

    — so the command the message exists to hand over could not be copied.
    Keys outside the wrap set render through ``_kv_line``, one per line,
    untouched (Devin Review on #1266).
    """
    location: Optional[str] = response.headers.get("Location", "")
    configured = (configured or "").rstrip("/")
    new_base = redirect_target(location or "", configured)

    detail: dict = {"code": "server_moved"}
    if new_base:
        detail["moved_to"] = new_base
        detail["fix"] = f"AGNES_SERVER={new_base} agnes <command>"
        detail["config"] = f"server: {new_base}"
        detail["hint"] = (
            f"{configured} answered HTTP {response.status_code} and that address has moved. "
            "Redirects are not followed automatically — credentials are stripped on a "
            "cross-origin hop, so the retry would fail as 'not authenticated' rather than "
            "work. Use the one-off command above, or set the config line in your agnes "
            "config.yaml."
        )
    else:
        detail["hint"] = moved_server_message(response.status_code, location or "", configured)
    return {"detail": detail}

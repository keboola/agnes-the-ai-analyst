"""`server.json` — the MCP Registry listing for this server (CON-6).

Publishing to the community MCP Registry (and from there to clients that read
it, e.g. VS Code's MCP browser) is done with `mcp-publisher publish`, which
validates against
``https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json``.

These are offline guards for the constraints that are easy to trip and only
report at publish time — most of all the **100-character description cap**,
which is short enough that an ordinary edit walks past it. Schema-shape
validation proper belongs to the publisher CLI; nothing here fetches the
schema, so the suite stays network-free.

The listing is a URL TEMPLATE, not a fixed address: Agnes is self-hosted, so
every deployment answers on its own host and the registry's `variables`
mechanism is what lets one entry serve all of them. That is the part worth
guarding — a well-meaning edit that hard-codes someone's hostname would ship
a listing pointing every reader at one company's instance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

MANIFEST = Path("server.json")

#: From the registry schema (2025-12-11).
_NAME_RE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
_MAX_NAME = 200
_MAX_TITLE = 100
_MAX_DESCRIPTION = 100
_REMOTE_TYPES = {"streamable-http", "sse"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST.exists(), "server.json is the registry listing — do not delete it"
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_required_fields_are_present(manifest):
    for field in ("name", "description", "version"):
        assert manifest.get(field), f"{field} is required by the registry schema"


def test_description_fits_the_hundred_character_cap(manifest):
    """The cap the registry enforces and a reviewer never sees coming."""
    n = len(manifest["description"])
    assert n <= _MAX_DESCRIPTION, (
        f"description is {n} chars; the registry rejects anything over "
        f"{_MAX_DESCRIPTION}. Say what it does, not how it works."
    )


def test_name_matches_the_reverse_dns_pattern(manifest):
    name = manifest["name"]
    assert len(name) <= _MAX_NAME
    assert _NAME_RE.match(name), f"{name!r} must be reverse-DNS with exactly one slash, e.g. io.github.<org>/<server>"
    assert name.count("/") == 1


def test_title_fits_its_cap(manifest):
    if "title" in manifest:
        assert len(manifest["title"]) <= _MAX_TITLE


def test_version_is_a_point_release_not_a_range(manifest):
    """`^1.2`, `1.x` and `latest` are rejected — versions are immutable, and an
    update means publishing a new one."""
    v = manifest["version"]
    assert not any(c in v for c in "^~*"), f"{v!r} is a range, not a version"
    assert v != "latest"


def test_the_remote_is_streamable_http(manifest):
    remotes = manifest.get("remotes") or []
    assert remotes, "a remote server needs a `remotes` entry, not `packages`"
    assert remotes[0]["type"] in _REMOTE_TYPES
    assert remotes[0]["type"] == "streamable-http", "SSE is deprecated; this server speaks streamable HTTP"


def test_the_url_stays_a_template_with_every_placeholder_declared(manifest):
    """The self-hosted invariant.

    One listing serves every deployment only while the host is a variable the
    reader fills in. A hard-coded hostname here would point all of them at one
    instance, and an undeclared placeholder would render literally in the URL.
    """
    remote = manifest["remotes"][0]
    url = remote["url"]
    placeholders = set(re.findall(r"\{(\w+)\}", url))
    assert placeholders, (
        "the URL has no template variable — Agnes is self-hosted, so a fixed "
        "URL would send every reader to whichever instance was hard-coded"
    )
    declared = set((remote.get("variables") or {}).keys())
    assert placeholders == declared, (
        f"URL placeholders {sorted(placeholders)} do not match declared variables {sorted(declared)}"
    )
    for var in declared:
        assert remote["variables"][var].get("description"), (
            f"variable {var!r} needs a description — it is the prompt the reader answers"
        )


def test_the_url_points_at_the_streamable_endpoint(manifest):
    """Guards against the listing drifting onto the deprecated SSE path."""
    url = manifest["remotes"][0]["url"]
    assert url.startswith("https://"), "the registry requires https for a remote"
    assert url.endswith("/api/mcp/http"), (
        "the OAuth-protected streamable endpoint is /api/mcp/http; /api/mcp/sse is the legacy one"
    )

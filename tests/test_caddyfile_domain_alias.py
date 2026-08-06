"""Contract: the optional legacy-domain redirect block in the main Caddyfile.

`DOMAIN_ALIAS` lets a deployment keep answering on the hostname it is
migrating away from — Caddy serves that name and 301s onto `DOMAIN` — so a
domain cutover doesn't break old bookmarks, `agnes` CLI configs, and MCP
connector URLs with a TLS handshake failure.

Two properties are easy to break by a well-meaning edit and fatal in
production, hence text assertions here (no Caddy binary needed):

1. The default address must stay NON-PUBLIC. Any resolvable default would make
   every Agnes deployment attempt an ACME issuance for someone else's name on
   each start.
2. The block must not inherit the primary site's cert-FILE default. A missing
   `/certs/fullchain.pem` is fatal at startup, so a cert-file fallback on a
   block nobody configured would take the primary site down with it.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CADDYFILE = _ROOT / "Caddyfile"


def _alias_block(text: str) -> str:
    """Return the body of the `{$DOMAIN_ALIAS:...} { ... }` site block."""
    m = re.search(r"^\{\$DOMAIN_ALIAS:[^}]*\}\s*\{", text, re.MULTILINE)
    assert m, "expected a `{$DOMAIN_ALIAS:<default>} {` site block in the Caddyfile"
    depth = 1
    i = m.end()
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[m.start() : i]


def test_alias_block_redirects_to_the_primary_domain():
    block = _alias_block(_CADDYFILE.read_text())
    assert "redir https://{$DOMAIN:localhost}{uri} permanent" in block, (
        "the DOMAIN_ALIAS block must 301 onto {$DOMAIN} carrying {uri} (path + query) through unchanged"
    )


def test_only_browser_navigation_gets_the_notice_page():
    """The notice page is for people; machine clients must get the 301.

    Verified live: `Accept: text/html` GET → 200 HTML, default-Accept GET →
    301, and `POST` with `Accept: text/html` → 301 (so a state-changing call
    is never swallowed by an HTML page).
    """
    block = _alias_block(_CADDYFILE.read_text())
    matcher = re.search(r"@browser\s*\{(.*?)\}", block, re.DOTALL)
    assert matcher, "expected an @browser matcher scoping the notice page"
    body = matcher.group(1)
    assert re.search(r"^\s*method GET HEAD\s*$", body, re.MULTILINE), (
        "the notice page must be scoped to GET/HEAD — a POST answered with "
        "HTML instead of a redirect silently drops the request"
    )
    assert "header Accept *text/html*" in body, (
        "the split between the notice page and the 301 must key on Accept: "
        "text/html — browsers send it on navigation, API clients do not"
    )
    assert "handle @browser" in block and re.search(r"handle\s*\{", block), (
        "expected both branches: `handle @browser` (notice) and a bare `handle` (301 fallback for everything else)"
    )


def test_notice_page_carries_the_deep_link_and_no_css_braces():
    """`respond` substitutes placeholders in the body, so a CSS rule's braces
    would be read as placeholder syntax — every style must be inline."""
    block = _alias_block(_CADDYFILE.read_text())
    respond = block[block.find("respond <<HTML") :]
    assert "<style" not in respond, (
        "no <style> block in the notice page — Caddy substitutes placeholders "
        "in a respond body and CSS braces read as placeholder syntax; use "
        "inline style attributes"
    )
    assert respond.count("https://{$DOMAIN:localhost}{uri}") >= 2, (
        "the notice page must carry {uri} into BOTH the meta refresh and the link, so a deep link survives the hop"
    )


def test_alias_default_is_not_a_public_hostname():
    text = _CADDYFILE.read_text()
    m = re.search(r"^\{\$DOMAIN_ALIAS:([^}]*)\}", text, re.MULTILINE)
    assert m, "expected `{$DOMAIN_ALIAS:<default>}` as the alias site address"
    default = m.group(1)
    host = default.rsplit(":", 1)[0] if ":" in default else default
    assert host in ("localhost", "127.0.0.1", "[::1]"), (
        f"DOMAIN_ALIAS default {default!r} must be a non-public address — a "
        "resolvable default makes every deployment attempt an ACME issuance "
        "for it on startup"
    )


def test_alias_block_has_no_explicit_tls_directive():
    """Verified live against caddy:2-alpine, both ways.

    An explicit `tls <email>` pins an ACME issuer for the site and overrides
    Caddy's per-name issuer choice, so the inert localhost default loops on
    "subject 'localhost' does not qualify for a public certificate" every 60 s
    — on every deployment that never set DOMAIN_ALIAS. Left implicit, the same
    run logs "certificate obtained successfully, issuer: local".
    """
    block = _alias_block(_CADDYFILE.read_text())
    # Comments in this block exist precisely to explain the absence — assert
    # against the directives only.
    directives = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "CADDY_TLS" not in directives, (
        "the DOMAIN_ALIAS block must NOT carry a tls directive — inheriting "
        "{$CADDY_TLS} makes every alias-less deployment retry a public "
        "certificate for the localhost default once a minute, forever"
    )
    assert not re.search(r"^\s*tls\s", directives, re.MULTILINE), (
        "no explicit `tls` directive in the DOMAIN_ALIAS block — automatic "
        "HTTPS must pick the issuer per name (internal for the localhost "
        "default, public ACME for a real alias hostname)"
    )


def test_compose_passes_domain_alias_through_without_a_default():
    text = (_ROOT / "docker-compose.yml").read_text()
    assert re.search(r"^\s*- DOMAIN_ALIAS$", text, re.MULTILINE), (
        "docker-compose.yml's caddy service must pass DOMAIN_ALIAS through "
        "bare (`- DOMAIN_ALIAS`); assigning a default would set the variable "
        "to an empty string, substituting an EMPTY site address that Caddy "
        "refuses to parse"
    )


def test_startup_script_omits_the_env_line_when_unset():
    """The module must not write `DOMAIN_ALIAS=` for a VM without an alias."""
    tpl = (_ROOT / "infra" / "modules" / "customer-instance" / "startup-script.sh.tpl").read_text()
    assert 'DOMAIN_ALIAS_LINE=""' in tpl and 'if [ -n "$DOMAIN_ALIAS" ]; then' in tpl, (
        "startup-script.sh.tpl must guard the DOMAIN_ALIAS .env line on a "
        "non-empty value — Caddy's `{$DOMAIN_ALIAS:default}` fallback applies "
        "only when the variable is UNSET"
    )
    assert "$DOMAIN_ALIAS_LINE" in tpl, "the guarded line must reach the .env heredoc"

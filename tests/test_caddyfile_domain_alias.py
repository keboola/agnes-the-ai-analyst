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
    assert "redir https://{$DOMAIN:localhost}{uri} 308" in block, (
        "the DOMAIN_ALIAS block must 301 onto {$DOMAIN} carrying {uri} (path + query) through unchanged"
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


def test_the_redirect_preserves_method_and_body():
    """A 301 on a POST is re-issued by clients as a GET with the body dropped.
    This feature exists to keep `agnes push` and MCP JSON-RPC — both POSTs —
    working through a domain cutover, so a 301 would break exactly what it
    promises to carry. 308 is equally permanent and preserves both
    (Devin Review on #1182)."""
    from pathlib import Path

    caddy = Path("Caddyfile").read_text(encoding="utf-8")
    assert "{uri} 308" in caddy, "the alias redirect must be 308, not 301/permanent"
    assert "{uri} permanent" not in caddy, "301 drops POST bodies"


def test_the_alias_cannot_be_set_equal_to_the_domain():
    """Two site blocks with the same address make Caddy refuse to parse its
    config, which takes the PRIMARY site down on the next reload — so this is
    rejected at plan time, and again in the startup script for a value that
    reached the instance another way."""
    from pathlib import Path

    tf = Path("infra/modules/customer-instance/variables.tf").read_text(encoding="utf-8")
    assert tf.count("domain_alias must differ from") >= 1
    # both carriers of the field are covered
    assert "var.prod_instance.domain_alias" in tf
    assert "for i in var.dev_instances" in tf

    sh = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text(encoding="utf-8")
    assert '[ "$DOMAIN_ALIAS" != "$DOMAIN" ]' in sh, "startup script writes the line without checking"

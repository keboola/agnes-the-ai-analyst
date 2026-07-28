"""Text-assertion contract for `deploy/caddy/Caddyfile.mtier` (spec §3.7:
"the proxy never removes the last healthy upstream ... falls through to a
maintenance page instead of hard-503ing everything").

No Caddy binary/Docker dependency here (the reference validation — `caddy
validate`/`caddy adapt` against a running container — was done manually
during development, see the PR); these are lightweight assertions on the
file's structure so a future edit can't silently drop the error-handling
block or the pre-existing `/metrics` deny rule.
"""

from __future__ import annotations

from pathlib import Path

_CADDYFILE = Path(__file__).resolve().parent.parent / "deploy" / "caddy" / "Caddyfile.mtier"


def _text() -> str:
    return _CADDYFILE.read_text()


def test_metrics_still_denied_with_404():
    text = _text()
    assert "@metrics path /metrics" in text
    assert "respond @metrics 404" in text


def test_handle_errors_covers_bad_gateway_and_unavailable_statuses():
    text = _text()
    assert "handle_errors 502 503 504" in text


def test_handle_errors_serves_static_html_maintenance_page_not_hard_503():
    text = _text()
    # Must respond with an actual body (a maintenance page), not bare
    # `respond 503` (Caddy's default empty-body error).
    assert "body <<HTML" in text
    assert "<!doctype html>" in text.lower() or "<!DOCTYPE html>" in text
    assert "temporarily unavailable" in text.lower()
    # Explicit 503 status on the maintenance response itself.
    assert "respond 503 {" in text


def test_gateway_and_api_reverse_proxy_rules_untouched():
    text = _text()
    assert "reverse_proxy @gateway gateway:8000" in text
    assert "reverse_proxy api1:8000 api2:8000" in text
    assert "lb_policy round_robin" in text

"""API design rule enforcement — prevents new violations from accumulating.

Existing violations are captured in allowlists: visible, deliberate,
and documented so they can be shrunk over time.

See: https://github.com/keboola/agnes-the-ai-analyst/issues/337
"""

import os
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "openapi.json"
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spec():
    """Boot the app in test mode — same fixture strategy as test_openapi_snapshot."""
    os.environ.setdefault("TESTING", "1")
    from app.main import create_app

    return create_app().openapi()


def _ops(spec):
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method in _HTTP_METHODS:
                yield path, method, op


# ---------------------------------------------------------------------------
# Rule 1 — No new verbs in URL path segments
#
# Rationale: verb-in-URL encodes intent in the path rather than the HTTP method,
# which breaks REST client assumptions, prevents generic caching/retry logic,
# and makes the API surface harder to discover.
#
# Exceptions: RPC-style command-bus operations where the HTTP method genuinely
# cannot express the intent (e.g. fire-and-forget triggers, state machines).
# These are explicitly listed below so the allowlist is self-documenting.
# ---------------------------------------------------------------------------

_VERBS = frozenset({
    "trigger", "run", "activate", "deactivate", "approve", "reject", "revoke",
    "register", "discover", "refresh", "reset", "send", "import", "export",
    "push", "pull", "enable", "disable", "rebuild", "reload", "bulk", "precheck",
    "rescan",
})

# Existing violations — grandfathered. Do not extend this list.
# Each entry should include a brief note on why it is intentional RPC.
_VERB_PATH_ALLOWLIST = frozenset({
    # Command-bus triggers — fire-and-forget, no idiomatic REST resource
    "/api/sync/trigger",
    "/api/scripts/run",
    "/api/scripts/run-due",
    "/api/scripts/{script_id}/run",
    "/api/marketplaces/{marketplace_id}/sync",
    "/api/marketplaces/sync-all",
    # State transitions on governance resources
    "/api/memory/admin/approve",
    "/api/memory/admin/reject",
    "/api/memory/admin/revoke",
    "/api/memory/admin/bulk-update",
    # Memory-domain suggestion lifecycle — pending → approved/rejected
    # state-machine. Approve also creates the real memory_domains row as
    # a side effect, so it's not a clean PATCH on a single field.
    "/api/admin/memory-domain-suggestions/{sid}/approve",
    "/api/admin/memory-domain-suggestions/{sid}/reject",
    # User lifecycle — activate/deactivate map to a boolean field (acceptable PATCH candidate)
    "/api/users/{user_id}/activate",
    "/api/users/{user_id}/deactivate",
    "/api/users/{user_id}/reset-password",
    # Admin operations — discovery + registration (complex multi-step, no single resource)
    "/api/admin/discover-and-register",
    "/api/admin/discover-tables",
    "/api/admin/register-table",
    "/api/admin/register-table/precheck",
    "/api/admin/metadata/{table_id}/push",
    "/api/admin/metrics/import",
    # Profile refresh — triggers async re-profiling of table metadata
    "/api/catalog/profile/{table_name}/refresh",
    # BQ metadata cache refresh — on-demand operator trigger for a single registry row
    "/api/v2/metadata-cache/refresh",
    # Cache warmup — manual trigger (idempotent fire-and-forget)
    "/api/admin/cache-warmup/run",
    # Store submission rescan — re-runs guardrail scan on an existing submission
    "/api/admin/store/submissions/{submission_id}/rescan",
    # Telemetry export — GET because it streams a report, not a resource collection
    "/api/admin/telemetry/export",
    # Auth flows — /auth/* uses verb-style paths by convention across the industry
    "/auth/email/send-link",
    "/auth/password/reset",
    "/auth/password/reset/confirm",
    "/auth/password/setup",
    "/auth/password/setup/confirm",
    "/auth/password/setup/request",
    # Sync sub-resources — "sync" is the resource namespace here, not the verb
    "/api/sync/manifest",
    "/api/sync/settings",
    "/api/sync/table-subscriptions",
})


def test_no_new_verbs_in_path(spec):
    """New path segments must not contain action verbs."""
    violations = []
    for path, method, _ in _ops(spec):
        if path in _VERB_PATH_ALLOWLIST:
            continue
        segs = [s for s in path.split("/") if s and not s.startswith("{")]
        hits = [s for s in segs if s.lower() in _VERBS]
        if hits:
            violations.append(f"  {method.upper():6} {path}  (verbs: {hits})")

    assert not violations, (
        f"{len(violations)} new verb-in-URL violation(s):\n" + "\n".join(violations) + "\n\n"
        "Fix: model the action as a resource state change (noun + HTTP method).\n"
        "If the operation is genuinely RPC (fire-and-forget, state machine), add to "
        "_VERB_PATH_ALLOWLIST with a comment explaining why."
    )


# ---------------------------------------------------------------------------
# Rule 2 — DELETE must return 204 No Content
#
# Rationale: DELETE is idempotent; 204 signals successful removal without a
# response body. Returning 200 with a body on DELETE conflates "removed" with
# "here is the removed representation" — which is a read concern, not a write one.
#
# No allowlist: the two pre-existing violations were fixed in this PR.
# ---------------------------------------------------------------------------


def test_delete_returns_204(spec):
    """DELETE operations must declare 204 No Content."""
    violations = []
    for path, method, op in _ops(spec):
        if method != "delete":
            continue
        codes = set(op.get("responses", {}).keys())
        if "204" not in codes:
            violations.append(f"  DELETE {path}  (declares: {sorted(codes)})")

    assert not violations, (
        f"{len(violations)} DELETE endpoint(s) not declaring 204:\n" + "\n".join(violations) + "\n\n"
        "Fix: return Response(status_code=204) and remove any response body.\n"
        "If the endpoint intentionally returns content after deletion, return 200 and "
        "add a response_model — then add it to an allowlist here with a comment."
    )


# ---------------------------------------------------------------------------
# Rule 3 — True creator POSTs must declare 201 Created
#
# Heuristic: a POST is a "creator" if the same path also has a GET method
# (i.e. it is a collection endpoint with read+write).  Pure RPC commands
# (/api/query, /api/sync/trigger) have no GET counterpart and are excluded.
#
# Allowlist: false positives from the heuristic (upserts, config saves,
# auth flows that respond with 200 by design).
# ---------------------------------------------------------------------------

_CREATOR_POST_ALLOWLIST = frozenset({
    # Config upserts — update existing config, not create a new resource
    "/api/admin/server-config",
    "/api/sync/settings",
    # Subscription upsert — sets per-table enabled flags, not a pure create
    "/api/sync/table-subscriptions",
    # Auth flows — 200 is conventional for token/session responses
    "/auth/email/verify",
    "/auth/password/reset",
    "/auth/password/setup",
    # Register/update upsert — saves config, not a pure create
    "/api/admin/initial-workspace",
    # Saved-view upsert — ON CONFLICT updates existing name rather than creating
    "/api/admin/observability/views",
})


def test_creator_post_declares_201(spec):
    """POST on a collection endpoint (path also has GET) must declare 201 or 202."""
    violations = []
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        if "post" not in methods or "get" not in methods:
            continue
        if path in _CREATOR_POST_ALLOWLIST:
            continue
        last = path.rstrip("/").split("/")[-1]
        if last.startswith("{"):
            continue  # item endpoint, not collection
        op = methods["post"]
        codes = set(op.get("responses", {}).keys())
        if "201" not in codes and "202" not in codes:
            violations.append(f"  POST {path}  (declares: {sorted(codes)})")

    assert not violations, (
        f"{len(violations)} creator POST(s) missing 201/202:\n" + "\n".join(violations) + "\n\n"
        "Fix: add responses={{201: {{...}}}} (sync create) or 202 (async create) to the decorator.\n"
        "If the POST is an upsert or config save rather than a create, add to "
        "_CREATOR_POST_ALLOWLIST with a comment."
    )


# ---------------------------------------------------------------------------
# Rule 4 — Protected /api/* endpoints must declare 401 and 403
#
# Rationale: auth errors are real contract elements. Clients (including LLMs)
# that read the spec to understand retry / fallback behaviour need to know
# these codes exist.  The declarations are injected centrally via
# _add_auth_error_responses() in app/main.py, so per-route boilerplate is
# not required.
#
# Public paths: intentionally unauthenticated (health probes, auth entry points).
# ---------------------------------------------------------------------------

_PUBLIC_API_PATHS = frozenset({
    "/api/health",
    "/api/health/detailed",
    "/api/version",
})


def test_protected_endpoints_declare_auth_errors(spec):
    """Every /api/* endpoint not in PUBLIC must declare 401 and 403."""
    violations = []
    for path, method, op in _ops(spec):
        if not path.startswith("/api/"):
            continue
        if path in _PUBLIC_API_PATHS:
            continue
        codes = set(op.get("responses", {}).keys())
        missing = [c for c in ("401", "403") if c not in codes]
        if missing:
            violations.append(
                f"  {method.upper():6} {path}  (missing: {', '.join(missing)})"
            )

    assert not violations, (
        f"{len(violations)} protected endpoint(s) missing auth error declarations:\n"
        + "\n".join(violations[:40])
        + ("\n  … (truncated)" if len(violations) > 40 else "")
        + "\n\nFix: ensure the path is covered by _add_auth_error_responses() in app/main.py, "
        "or add to _PUBLIC_API_PATHS above if it is genuinely unauthenticated."
    )

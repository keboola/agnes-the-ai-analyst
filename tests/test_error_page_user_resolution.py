"""``_resolve_error_user`` (app/main.py) must resolve the authenticated user
for the rendered HTML error page without opening the process-singleton
DuckDB connection — see the ``_GRANDFATHERED_GET_SYSTEM_DB`` cleanup in
``tests/test_backend_split_guard.py``. Every helper ``get_current_user``
calls already treats ``conn=None`` as "resolve through the backend-aware
factory"; this locks in both the source-level gate and the actual
end-to-end behavior (the error page still shows the authenticated user).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "app" / "main.py"


def _nested_function_source(outer_name: str, inner_name: str) -> str:
    src = MAIN_PY.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == outer_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.AsyncFunctionDef) and inner.name == inner_name:
                    seg = ast.get_source_segment(src, inner)
                    assert seg is not None
                    return seg
    raise AssertionError(f"expected a nested def {inner_name}(...) inside {outer_name}()")


def test_resolve_error_user_never_opens_duckdb():
    """Static gate: `_resolve_error_user` must not call `get_system_db()` at
    all — every downstream helper `get_current_user` calls already resolves
    through the factory when `conn` is `None`."""
    seg = _nested_function_source("create_app", "_resolve_error_user")
    assert "get_system_db()" not in seg
    assert "conn=None" in seg


def test_error_page_shows_authenticated_user(seeded_app):
    """Behavioral check: hitting a 403/404 with an HTML Accept header and a
    valid bearer token still renders the user's identity in the error page
    chrome — `_resolve_error_user` returning early on `conn=None` must not
    silently drop the user."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get(
        "/this-path-does-not-exist",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/html",
        },
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "admin@test.com" in resp.text

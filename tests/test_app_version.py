"""Pin that the FastAPI `version=` is read dynamically from package metadata.

The OpenAPI schema (`/openapi.json`, `/docs`) advertises this version. A
hardcoded literal — the previous state — silently drifts from
`pyproject.toml` on every bump, leaving `/openapi.json` reporting a stale
version while `/api/version`, `/cli/latest`, and `da --version` all
report the bumped one.
"""

from unittest.mock import patch


def test_app_version_reads_package_metadata():
    """`_app_version()` must call importlib.metadata.version with the
    canonical package name, not return a hardcoded literal."""
    with patch("app.main._pkg_version", return_value="9.9.9") as mock_pkg_ver:
        from app.main import _app_version
        assert _app_version() == "9.9.9"
        mock_pkg_ver.assert_called_once_with("agnes-the-ai-analyst")


def test_app_version_falls_back_to_dev_when_package_missing():
    """Source-checkout without install → report 'dev', not crash."""
    from importlib.metadata import PackageNotFoundError
    with patch("app.main._pkg_version", side_effect=PackageNotFoundError):
        from app.main import _app_version
        assert _app_version() == "dev"


def test_fastapi_app_version_matches_package_metadata():
    """End-to-end: what FastAPI stores in `app.version` is whatever
    `_app_version()` returned — not a stale literal."""
    with patch("app.main._pkg_version", return_value="7.7.7"):
        from app.main import create_app
        app = create_app()
        assert app.version == "7.7.7"

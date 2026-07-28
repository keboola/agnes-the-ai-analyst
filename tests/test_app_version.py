"""Pin that APP_VERSION reads from package metadata, not a hardcoded literal,
and that the FastAPI app's `version=` field surfaces it end-to-end."""

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture
def _restore_app_modules():
    """Reload-with-real-metadata so subsequent tests see the genuine
    APP_VERSION / FastAPI app instance, not the patched-in fake from this
    file's tests."""
    yield
    import app.version
    importlib.reload(app.version)
    import app.main
    importlib.reload(app.main)


def test_app_version_reads_package_metadata(_restore_app_modules):
    # Patch the source `importlib.metadata.version` rather than the alias
    # bound into app.version at import time — `importlib.reload(app.version)`
    # re-runs the `from importlib.metadata import version as _pkg_version`
    # line, which would otherwise re-fetch the unpatched original and
    # silently neuter the test.
    with patch("importlib.metadata.version", return_value="9.9.9") as mock_pkg_ver:
        import app.version
        importlib.reload(app.version)
        assert app.version.APP_VERSION == "9.9.9"
        # `assert_called_with` (not `assert_called_once_with`) — `import
        # app.version` may have triggered an initial load before reload,
        # giving two calls. We only care that the package name is canonical.
        mock_pkg_ver.assert_called_with("agnes-the-ai-analyst")


def test_app_version_falls_back_when_package_missing(_restore_app_modules):
    from importlib.metadata import PackageNotFoundError
    with patch("importlib.metadata.version", side_effect=PackageNotFoundError):
        import app.version
        importlib.reload(app.version)
        assert app.version.APP_VERSION == "0.0.0+dev"


def test_fastapi_app_version_matches_app_version_constant(_restore_app_modules):
    """End-to-end: FastAPI's app.version (consumed by /openapi.json and
    /docs) must equal app.version.APP_VERSION. Guards the wiring at
    `app/main.py:186 version=APP_VERSION` against accidental literal."""
    import app.version
    import app.main

    # Reload both so we read post-patch values consistently.
    with patch("importlib.metadata.version", return_value="7.7.7"):
        importlib.reload(app.version)
        importlib.reload(app.main)
        assert app.main.app.version == "7.7.7"
        assert app.main.app.version == app.version.APP_VERSION

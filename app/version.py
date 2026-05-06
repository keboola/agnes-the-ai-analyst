"""Single source of truth for app + CLI compat versions.

`APP_VERSION` is read from package metadata so it tracks `pyproject.toml`
without a manual literal to keep in sync.

`MIN_COMPAT_CLI_VERSION` is the oldest CLI version the server still accepts
on `/api/*`. Bumped manually when shipping a wire-protocol break. Day-one
value of "0.0.0" means no enforcement — set the floor the first time a
deliberate break ships.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def _read_app_version() -> str:
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "0.0.0+dev"


APP_VERSION = _read_app_version()
MIN_COMPAT_CLI_VERSION = "0.0.0"

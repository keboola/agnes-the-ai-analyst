"""Version constants used by FastAPI's `version=` field and the
`X-Agnes-{Latest,Min}-Version` response-header middleware.

`APP_VERSION` reads from package metadata so it tracks `pyproject.toml`
without a manual literal to keep in sync. **This is not a project-wide
single source of truth** — `AGNES_VERSION` env var (set by CI/Docker
builds) continues to drive `/api/version`, `/cli/install.sh`, and the
admin UI. Those call sites pre-date `app/version.py` and are out of scope
for this change.

`MIN_COMPAT_CLI_VERSION` is the oldest CLI version the server advertises
as compatible on `/api/*` response headers. Enforcement lives in the
client: `cli/client.py:_check_version_headers` exits the CLI when its
local version is below this floor. The middleware itself does not reject
requests — older clients just get a header they're free to ignore (in
practice, only the agnes CLI inspects it).

Day-one value of `MIN_COMPAT_CLI_VERSION` is `0.0.0` (no enforcement);
bumped manually when shipping a wire-protocol break.
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

# Comma-separated list of opt-in wire-protocol capabilities the server
# accepts, advertised on /api/* responses as `X-Agnes-Accepts`. Clients
# treat an absent header as "none" and fall back to legacy formats.
# `session-gzip`: POST /api/upload/sessions accepts a gzip-compressed
# transcript when the part filename ends in `.gz`.
SERVER_CAPABILITIES = "session-gzip"

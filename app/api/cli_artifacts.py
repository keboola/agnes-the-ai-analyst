"""CLI artifact download + install script endpoints (#9)."""

import os
import re
import shlex
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

# Strict allowlists for values interpolated into the generated install.sh.
# The endpoint is unauthenticated and users `curl | bash` it, so any shell
# metacharacter leaking through the Host header or AGNES_VERSION env var
# would become RCE. `shlex.quote` is applied on top for defense in depth.
#
# Host charset allows underscores (Docker Compose hostnames) and `[` `]` `:`
# so IPv6 literals like http://[::1]:8000 pass. Optional trailing path lets
# reverse-proxy deployments (request.base_url = "https://host/agnes/") work.
#
# `\Z` (not `$`) anchors strictly to end-of-string. Python's `$` also matches
# immediately before a trailing `\n`, which would let a crafted Host header
# like "good.example.com\n$(rm -rf /)" slip past the allowlist. `\Z` closes
# that bypass — shlex.quote downstream is still defense-in-depth.
_SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9._\-\[\]:]+(:\d+)?(/[A-Za-z0-9._\-/]*)?\Z")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._\-]+\Z")

router = APIRouter(tags=["cli"])


def _dist_dir() -> Path:
    return Path(os.environ.get("AGNES_CLI_DIST_DIR", "/app/dist"))


def _find_wheel() -> Path | None:
    d = _dist_dir()
    if not d.exists():
        return None
    wheels = sorted(d.glob("*.whl"))
    return wheels[-1] if wheels else None


@router.get("/cli/download")
async def cli_download():
    wheel = _find_wheel()
    if not wheel:
        raise HTTPException(
            status_code=404,
            detail=(
                "CLI wheel not found in dist dir. Build it with `uv build --wheel` "
                "or run the official docker image (which builds on image-build)."
            ),
        )
    return FileResponse(
        path=str(wheel),
        filename=wheel.name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{wheel.name}"'},
    )


@router.get("/cli/agnes.whl")
async def cli_wheel_stable():
    """Stable `.whl` URL alias so `uv tool install <server>/cli/agnes.whl` works.

    `uv tool install` inspects the URL path to decide how to treat the resource
    and only accepts it as a wheel when the path ends in `.whl`. The existing
    `/cli/download` path does not, which forces users through a multi-step
    curl + tmpfile + install + rm dance. This alias collapses that into a
    single `uv tool install` invocation.
    """
    wheel = _find_wheel()
    if not wheel:
        raise HTTPException(
            status_code=404,
            detail=(
                "CLI wheel not found in dist dir. Build it with `uv build --wheel` "
                "or run the official docker image (which builds on image-build)."
            ),
        )
    return FileResponse(
        path=str(wheel),
        filename=wheel.name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{wheel.name}"'},
    )


@router.get("/cli/install.sh", response_class=PlainTextResponse)
async def cli_install_script(request: Request):
    """Shell installer — bakes this server's URL into the generated config."""
    base_url = str(request.base_url).rstrip("/")
    if not _SAFE_URL_RE.match(base_url):
        raise HTTPException(status_code=400, detail="Unexpected server URL format")
    version = os.environ.get("AGNES_VERSION", "dev")
    if not _SAFE_VERSION_RE.match(version):
        version = "dev"
    # shlex.quote hardens against anything that slipped past the regex
    server_q = shlex.quote(base_url)
    version_q = shlex.quote(version)
    script = f"""#!/usr/bin/env bash
# Agnes CLI installer — server: {base_url}
set -euo pipefail

SERVER={server_q}
echo "Installing Agnes CLI from $SERVER (version: {version_q})"

# 1. Download the wheel
# Portable mktemp: X's must be at the end of the template on both GNU and BSD/macOS.
TMPDIR_WHEEL=$(mktemp -d -t agnes_cli.XXXXXX)
trap 'rm -rf "$TMPDIR_WHEEL"' EXIT
# Use -OJ so curl honours Content-Disposition and saves the wheel with its real
# PEP-427 filename (pip / uv tool install reject filenames without a version).
(cd "$TMPDIR_WHEEL" && curl -fsSL -OJ "$SERVER/cli/download")
WHEEL=$(ls "$TMPDIR_WHEEL"/*.whl 2>/dev/null | head -n1)
if [ -z "$WHEEL" ]; then
    echo "error: wheel download failed (no .whl found in $TMPDIR_WHEEL)" >&2
    exit 1
fi

# 2. Install via pip (prefer uv tool install if available)
if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$WHEEL"
else
    python3 -m pip install --user --force-reinstall "$WHEEL"
fi

# 3. Seed the server URL in CLI config
CFG_DIR="${{DA_CONFIG_DIR:-$HOME/.config/da}}"
mkdir -p "$CFG_DIR"
cat > "$CFG_DIR/config.yaml" <<EOF
server: $SERVER
EOF

echo "Installed."
echo "Next steps:"
echo "  1. Sign in to $SERVER and create a personal access token at $SERVER/profile"
echo "  2. Export it:   export DA_TOKEN=<your-token>"
echo "  3. Verify:      da auth whoami"
"""
    return script

<#
.SYNOPSIS
    Windows/PowerShell sibling of scripts/run-local-dev.sh.

.DESCRIPTION
    Runs Agnes locally with auth bypass + dev-mode magic links. Stacks three compose files:
      1. docker-compose.yml          - base services
      2. docker-compose.dev.yml - hot-reload + source bind mount
      3. docker-compose.local-dev.yml - LOCAL_DEV_MODE=1, drops .env requirement

    After startup visit http://localhost:8000 - you'll land on /dashboard logged in as
    dev@localhost (role=admin). No login screen, no email delivery needed.

    Source code is bind-mounted from the host, so Python changes are picked up by
    uvicorn --reload. Rebuild is only needed when pyproject.toml or Dockerfile change
    (e.g. after a `git pull` that adds deps). Use -Build for those cases.

.PARAMETER Action
    up    (default) docker compose up. The image is auto-built on first run.
    down  docker compose down (stop + remove containers; data volume preserved).
    logs  docker compose logs -f (tail).

.PARAMETER Build
    Force --build on `up`. Use after pulling changes that touch pyproject.toml or
    Dockerfile, or when you hit ModuleNotFoundError from a stale cached image.

.PARAMETER DataPath
    Optional Windows folder to use as the /data mount inside the container.
    When omitted, Compose falls back to the named volume `agnes-the-ai-analyst_data`
    which lives inside the Docker Desktop WSL VM (not directly visible on the
    host). When set, /data is bind-mounted to this folder so system.duckdb,
    extracts, marketplaces, store/, etc. are reachable from Windows Explorer.
    The folder is created if it doesn't exist; relative paths resolve against
    the operator's current shell directory. NOTE: switching DataPath between
    runs swaps the entire /data mount — the old named volume is preserved but
    not used; system.duckdb on the new path starts fresh on first boot.

.NOTES
    Anything else on the command line (e.g. -d, --remove-orphans) lands in
    PowerShell's automatic $args variable and is forwarded to docker compose.

.EXAMPLE
    .\scripts\run-local-dev.ps1
    # up - fast path; reuses existing image (auto-builds if none exists yet)

.EXAMPLE
    .\scripts\run-local-dev.ps1 -Build
    # up --build - force rebuild after dep / Dockerfile changes

.EXAMPLE
    .\scripts\run-local-dev.ps1 up -d
    # detached

.EXAMPLE
    .\scripts\run-local-dev.ps1 down
    # stop + remove containers (data volume preserved)

.EXAMPLE
    .\scripts\run-local-dev.ps1 logs
    # tail logs from the running stack

.EXAMPLE
    .\scripts\run-local-dev.ps1 -Build -DataPath C:\Business\Groupon\Agnes\agnes-data
    # bind /data to a Windows folder so DuckDB files are reachable from Explorer
#>
# Deliberately keep this a SIMPLE (non-advanced) script — no [CmdletBinding()]
# and no [Parameter(...)] attributes. Both promote the script to an advanced
# function, which auto-injects the common parameters (-Debug, -Verbose,
# -ErrorAction, ...) that PowerShell binds via prefix match. Documented
# examples like `up -d` (detached) and `down -v` (remove volumes) would
# silently have `-d` / `-v` eaten by `-Debug` / `-Verbose` instead of reaching
# docker compose. [ValidateSet(...)] and [switch] do NOT promote and stay.
# Unbound positional args land in PowerShell's automatic $args variable in a
# non-advanced script; we forward them to docker compose.
param(
    [ValidateSet('up', 'down', 'logs')]
    [string]$Action = 'up',

    [switch]$Build,

    [string]$DataPath
)

$ErrorActionPreference = 'Stop'

# Resolve $DataPath against the caller's PWD BEFORE the Push-Location below;
# otherwise relative paths would resolve against the repo root rather than the
# operator's shell. Folder is created if it doesn't exist. The path is
# normalized to forward slashes — Compose's short-syntax bind mount parser
# occasionally trips on backslashes on Windows.
$dataPathHost = $null
if ($DataPath) {
    if ([System.IO.Path]::IsPathRooted($DataPath)) {
        $dataPathHost = $DataPath
    } else {
        $dataPathHost = Join-Path (Get-Location).Path $DataPath
    }
    if (-not (Test-Path $dataPathHost)) {
        New-Item -ItemType Directory -Path $dataPathHost -Force | Out-Null
    }
    $dataPathHost = (Resolve-Path $dataPathHost).Path -replace '\\', '/'
}

# PowerShell scripts execute in the caller's runspace (unlike bash, which forks
# a child process), so Set-Location and $env:* assignments leak back into the
# user's shell after the script exits. Wrap the body in Push-Location /
# Pop-Location with try/finally and snapshot the LOCAL_DEV_GROUPS env-var so
# the operator's session is restored on any exit path (success, error, Ctrl+C
# during `up`/`logs`).
Push-Location (Split-Path -Parent $PSScriptRoot)
$localDevGroupsWasSet = Test-Path Env:LOCAL_DEV_GROUPS
$localDevGroupsOriginal = if ($localDevGroupsWasSet) { $env:LOCAL_DEV_GROUPS } else { $null }
$dataOverrideFile = $null
try {
    # docker-compose.yml declares env_file: .env on several services. Compose
    # validates that path even for profiled services that never start, so make
    # sure it exists.
    if (-not (Test-Path .env)) {
        New-Item -ItemType File -Path .env -Force | Out-Null
    }

    # Default LOCAL_DEV_GROUPS so /profile and group-aware code see *something* on
    # first boot. Mirrors scripts/run-local-dev.sh. Override/disable:
    #   $env:LOCAL_DEV_GROUPS = '[...]'; .\scripts\run-local-dev.ps1
    #   $env:LOCAL_DEV_GROUPS = '';      .\scripts\run-local-dev.ps1   # exercise no-groups path
    # Test-Path on Env: distinguishes unset (apply default) from set-to-empty
    # (honor operator intent) — same contract as the bash sibling.
    if (-not $localDevGroupsWasSet) {
        $env:LOCAL_DEV_GROUPS = '[{"id":"local-dev-engineers@example.com","name":"Local Dev Engineers"},{"id":"local-dev-admins@example.com","name":"Local Dev Admins"}]'
    }

    $composeFiles = @(
        '-f', 'docker-compose.yml',
        '-f', 'docker-compose.dev.yml',
        '-f', 'docker-compose.local-dev.yml'
    )

    # When -DataPath is supplied, generate a transient compose override that
    # rebinds /data from the named volume to a Windows host folder so DuckDB
    # files are reachable from Explorer. Compose merges service.volumes by
    # container target, so listing the same /data target replaces the
    # named-volume mount inherited from docker-compose.yml. Caddy's /srv:ro
    # readonly mirror is rebound the same way so the file_server keeps working.
    if ($dataPathHost) {
        $dataOverrideFile = Join-Path $env:TEMP "agnes-data-override-$PID.yml"
        $overrideYaml = @"
services:
  app:
    volumes:
      - ${dataPathHost}:/data
  scheduler:
    volumes:
      - ${dataPathHost}:/data
  extract:
    volumes:
      - ${dataPathHost}:/data
  caddy:
    volumes:
      - ${dataPathHost}:/srv:ro
"@
        # Use .NET WriteAllText so the file is BOM-less UTF-8 across PS 5.1 / 7+.
        [System.IO.File]::WriteAllText($dataOverrideFile, $overrideYaml)
        $composeFiles += @('-f', $dataOverrideFile)
        Write-Host "  /data is bind-mounted to host: $dataPathHost" -ForegroundColor Yellow
    }

    switch ($Action) {
        'up' {
            $cmd = @('up')
            if ($Build) { $cmd += '--build' }
        }
        'down' {
            $cmd = @('down')
        }
        'logs' {
            $cmd = @('logs', '-f')
        }
    }

    if ($args) { $cmd += $args }

    Write-Host "> docker compose $($composeFiles + $cmd -join ' ')" -ForegroundColor Cyan
    & docker compose @composeFiles @cmd
} finally {
    Pop-Location
    if ($localDevGroupsWasSet) {
        $env:LOCAL_DEV_GROUPS = $localDevGroupsOriginal
    } else {
        Remove-Item Env:LOCAL_DEV_GROUPS -ErrorAction SilentlyContinue
    }
    if ($dataOverrideFile -and (Test-Path $dataOverrideFile)) {
        Remove-Item $dataOverrideFile -Force -ErrorAction SilentlyContinue
    }
}

exit $LASTEXITCODE

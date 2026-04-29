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

    [switch]$Build
)

$ErrorActionPreference = 'Stop'

# PowerShell scripts execute in the caller's runspace (unlike bash, which forks
# a child process), so Set-Location and $env:* assignments leak back into the
# user's shell after the script exits. Wrap the body in Push-Location /
# Pop-Location with try/finally and snapshot the LOCAL_DEV_GROUPS env-var so
# the operator's session is restored on any exit path (success, error, Ctrl+C
# during `up`/`logs`).
Push-Location (Split-Path -Parent $PSScriptRoot)
$localDevGroupsWasSet = Test-Path Env:LOCAL_DEV_GROUPS
$localDevGroupsOriginal = if ($localDevGroupsWasSet) { $env:LOCAL_DEV_GROUPS } else { $null }
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
}

exit $LASTEXITCODE

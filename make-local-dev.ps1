<#
.SYNOPSIS
    Windows/PowerShell equivalent of `make local-dev{,-down,-logs}`.

.DESCRIPTION
    Runs Agnes locally with auth bypass + dev-mode magic links. Stacks three compose files:
      1. docker-compose.yml          - base services
      2. docker-compose.override.yml - hot-reload + source bind mount
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

.PARAMETER ExtraArgs
    Anything else (e.g. -d, --remove-orphans) passes through to docker compose.

.EXAMPLE
    .\make-local-dev.ps1
    # up — fast path; reuses existing image (auto-builds if none exists yet)

.EXAMPLE
    .\make-local-dev.ps1 -Build
    # up --build — force rebuild after dep / Dockerfile changes

.EXAMPLE
    .\make-local-dev.ps1 up -d
    # detached

.EXAMPLE
    .\make-local-dev.ps1 down
    # stop + remove containers (data volume preserved)

.EXAMPLE
    .\make-local-dev.ps1 logs
    # tail logs from the running stack
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'logs')]
    [string]$Action = 'up',

    [switch]$Build,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = 'Stop'

# Run from the repo root regardless of where the user invoked from.
Set-Location $PSScriptRoot

# docker-compose.yml declares env_file: .env on several services. Compose
# validates that path even for profiled services that never start, so make
# sure it exists.
if (-not (Test-Path .env)) {
    New-Item -ItemType File -Path .env -Force | Out-Null
}

$composeFiles = @(
    '-f', 'docker-compose.yml',
    '-f', 'docker-compose.override.yml',
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

if ($ExtraArgs) { $cmd += $ExtraArgs }

Write-Host "> docker compose $($composeFiles + $cmd -join ' ')" -ForegroundColor Cyan
& docker compose @composeFiles @cmd
exit $LASTEXITCODE

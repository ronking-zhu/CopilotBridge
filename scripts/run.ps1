<#
.SYNOPSIS
    Start the Copilot Bridge: the app server AND the Dev Tunnel together, with
    staged startup logging. This is the orchestrated entry point (launcher.py).
.EXAMPLE
    .\scripts\run.ps1
    .\scripts\run.ps1 -Port 3978
#>
param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$py   = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Virtual environment not found - running install first..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "install.ps1")
}

if ($Port -gt 0) { $env:PORT = "$Port" }

& $py (Join-Path $root "server\launcher.py")

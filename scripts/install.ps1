<#
.SYNOPSIS
    Install the Copilot Bridge server on this machine:
      1. create the Python virtual environment (.venv)
      2. install the app server dependencies
      3. bundle the Dev Tunnel CLI into tools/  (so deployment is self-contained)
.EXAMPLE
    .\scripts\install.ps1
    .\scripts\install.ps1 -Reinstall
#>
param(
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"
$root   = Split-Path $PSScriptRoot -Parent
$server = Join-Path $root "server"
$venv   = Join-Path $root ".venv"
$py     = Join-Path $venv "Scripts\python.exe"
$tools  = Join-Path $root "tools"

Write-Host "[install 1/3] Python virtual environment" -ForegroundColor Cyan
if ($Reinstall -and (Test-Path $venv)) {
    Write-Host "  removing existing .venv ..." -ForegroundColor DarkGray
    Remove-Item -Recurse -Force $venv
}
if (-not (Test-Path $py)) {
    python -m venv $venv
}

Write-Host "[install 2/3] App server dependencies" -ForegroundColor Cyan
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install -r (Join-Path $server "requirements.txt")

Write-Host "[install 3/3] Bundling the Dev Tunnel CLI into tools/" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$devtunnel = Join-Path $tools "devtunnel.exe"
if (Test-Path $devtunnel) {
    Write-Host "  already bundled: $devtunnel" -ForegroundColor DarkGray
}
else {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win-arm64" } else { "win-x64" }
    $url = "https://aka.ms/TunnelsCliDownload/$arch"
    Write-Host "  downloading devtunnel ($arch) from $url ..." -ForegroundColor DarkGray
    Invoke-WebRequest -Uri $url -OutFile $devtunnel
    Write-Host "  bundled: $devtunnel" -ForegroundColor Green
}

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "Next:" -ForegroundColor Yellow
Write-Host "  1. Copy server\.env.example to server\.env and fill it in (set CHAT_API_TOKEN)." -ForegroundColor Yellow
Write-Host "  2. One-time tunnel sign-in:  .\tools\devtunnel.exe user login" -ForegroundColor Yellow
Write-Host "  3. Start everything:         .\scripts\run.ps1" -ForegroundColor Yellow

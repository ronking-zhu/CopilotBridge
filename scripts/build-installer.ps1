<#
.SYNOPSIS
    Build the single-file Windows installer (setup .exe) for the Copilot Bridge
    server, so end users get ONE installer instead of a folder.

.DESCRIPTION
    1. Builds the self-contained server (onedir) via build-server-exe.ps1 unless
       -SkipBuild is given and the build already exists.
    2. Compiles installer\CopilotBridgeServer.iss with Inno Setup (ISCC.exe) into
       dist\CopilotBridgeServer-<version>-setup.exe.

    The resulting installer puts CopilotBridgeServer.exe + its runtime +
    devtunnel.exe into Program Files, adds the install folder to the system PATH,
    creates Start-menu/desktop shortcuts, and can launch the server (which shows
    the Dev Tunnel sign-in dialog) right after install.

.PARAMETER Version
    Product version embedded in the installer + output filename (default 1.5.0).

.EXAMPLE
    .\scripts\build-installer.ps1
    .\scripts\build-installer.ps1 -Version 1.2.0 -SkipBuild
#>
param(
    [string]$Version = "1.7.1",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root    = Split-Path $PSScriptRoot -Parent
$onedir  = Join-Path $root "dist\CopilotBridgeServer"
$exe     = Join-Path $onedir "CopilotBridgeServer.exe"
$iss     = Join-Path $root "installer\CopilotBridgeServer.iss"

# 1) Build the server application folder.
if (-not $SkipBuild -or -not (Test-Path $exe)) {
    Write-Host "[1/2] Building the server application (onedir)..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "build-server-exe.ps1")
}
else {
    Write-Host "[1/2] Reusing existing build: $exe" -ForegroundColor DarkGray
}
if (-not (Test-Path $exe)) { throw "Server build not found at $exe." }

# 2) Locate the Inno Setup compiler (ISCC.exe).
$iscc = $null
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $iscc = $c; break } }
if (-not $iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) {
    throw "Inno Setup (ISCC.exe) not found. Install it:  winget install JRSoftware.InnoSetup"
}
Write-Host "[2/2] Compiling installer with $iscc ..." -ForegroundColor Cyan

& $iscc `
    "/DAppVersion=$Version" `
    "/DSourceDir=$onedir" `
    "/DOutputDir=$(Join-Path $root 'dist')" `
    $iss

$setup = Join-Path $root "dist\CopilotBridgeServer-$Version-setup.exe"
if (Test-Path $setup) {
    $mb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "`nInstaller built: $setup ($mb MB)" -ForegroundColor Green
    Write-Host "Ship this single file. It installs to Program Files, sets PATH, and" -ForegroundColor Green
    Write-Host "launches the server (Dev Tunnel sign-in dialog) after install." -ForegroundColor Green
}
else {
    throw "ISCC finished but $setup was not produced. Check the output above."
}

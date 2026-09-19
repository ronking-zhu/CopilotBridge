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
    the Dev Tunnel sign-in dialog and browser notification/OneDrive setup) right after install.

.PARAMETER Version
    Product version embedded in the installer + output filename (default 2.6.7).

.EXAMPLE
    .\scripts\build-installer.ps1
    .\scripts\build-installer.ps1 -Version 1.2.0 -SkipBuild
#>
param(
    [string]$Version = "2.6.7",
    [string]$Edition = "ms",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root    = Split-Path $PSScriptRoot -Parent
$onedir  = Join-Path $root "dist\CopilotBridgeServer"
$exe     = Join-Path $onedir "CopilotBridgeServer.exe"
$iss     = Join-Path $root "installer\CopilotBridgeServer.iss"
$versionFile = Join-Path $root "server\version.py"
$buildVersionFile = Join-Path $onedir "build-version.txt"

$versionMatch = Select-String -LiteralPath $versionFile `
    -Pattern '^__version__\s*=\s*"([^"]+)"\s*$' | Select-Object -First 1
if (-not $versionMatch) { throw "Product version not found in $versionFile." }
$sourceVersion = $versionMatch.Matches[0].Groups[1].Value
if ($sourceVersion -ne $Version) {
    throw "Requested installer version $Version does not match source version $sourceVersion."
}

if ($Edition) {
    $setup = Join-Path $root "dist\copilotbridgeserver-$Edition-$Version-setup.exe"
} else {
    $setup = Join-Path $root "dist\CopilotBridgeServer-$Version-setup.exe"
}
if (Test-Path -LiteralPath $setup) {
    throw "Refusing to overwrite existing installer '$setup'. Use a new -Version."
}

# 1) Build the server application folder.
if (-not $SkipBuild -or -not (Test-Path $exe)) {
    Write-Host "[1/2] Building the server application (onedir)..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "build-server-exe.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Server build failed with exit code $LASTEXITCODE." }
}
else {
    Write-Host "[1/2] Reusing existing build: $exe" -ForegroundColor DarkGray
}
if (-not (Test-Path $exe)) { throw "Server build not found at $exe." }
if (-not (Test-Path -LiteralPath $buildVersionFile -PathType Leaf)) {
    throw "Server build version marker not found at $buildVersionFile. Rebuild without -SkipBuild."
}
$buildVersion = (Get-Content -LiteralPath $buildVersionFile -Raw).Trim()
if ($buildVersion -ne $Version) {
    throw "Server build version $buildVersion does not match installer version $Version."
}

foreach ($stateName in @('.env', 'connection.json', '.setup-done', 'portable', 'sessions', 'workspace', 'secrets')) {
    if (Test-Path -LiteralPath (Join-Path $onedir $stateName)) {
        throw "Refusing to package machine-local state '$stateName'. Rebuild a clean server payload."
    }
}

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
    "/DEdition=$Edition" `
    "/DSourceDir=$onedir" `
    "/DOutputDir=$(Join-Path $root 'dist')" `
    $iss
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $setup) { Remove-Item -LiteralPath $setup -Force }
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

if (Test-Path -LiteralPath $setup -PathType Leaf) {
    $mb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "`nInstaller built: $setup ($mb MB)" -ForegroundColor Green
    Write-Host "Ship this single file. It installs to Program Files, sets PATH, and" -ForegroundColor Green
    Write-Host "launches the server and notification/OneDrive setup after install." -ForegroundColor Green
}
else {
    throw "ISCC finished but $setup was not produced. Check the output above."
}

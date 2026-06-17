<#
.SYNOPSIS
    Package the Copilot Bridge **app server** into a self-contained Windows
    application folder (no Python install needed on the target machine).

.DESCRIPTION
    Builds ``dist\CopilotBridgeServer\CopilotBridgeServer.exe`` from
    server/launcher.py with PyInstaller. Uses **onedir** by default (a folder
    containing the exe + its runtime) because it's far more reliable than a
    single file — onefile extracts a Python DLL to %TEMP% at every launch, which
    antivirus / locked-down temp folders frequently block ("Failed to load
    Python DLL"). To distribute, zip the whole ``CopilotBridgeServer`` folder.
    Pass ``-OneFile`` if you specifically want a single .exe and your environment
    allows temp extraction.

    The app is fully self-configuring on first run:

      * generates a host-unique CHAT_API_TOKEN and saves it to a .env next to
        the exe (so every machine gets its own key),
      * starts the Dev Tunnel and prints the public URL + API key (the exact
        values to paste into a client),
      * serves the mobile web app and the chat API.

    Writable state (.env, connection.json, sessions\, workspace\) is created
    next to the exe; the web app assets are bundled inside it.

    NOTE: the Copilot/Claude CLIs and the `devtunnel` binary are NOT bundled —
    they're external tools the server drives. Install the CLI you want and (for
    the public URL) sign in once with `devtunnel user login`.

.EXAMPLE
    .\scripts\build-server-exe.ps1
    # then run:  .\dist\CopilotBridgeServer\CopilotBridgeServer.exe
#>
param(
    [string]$Name = "CopilotBridgeServer",
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$py   = Join-Path $root ".venv\Scripts\python.exe"
$entry = Join-Path $root "server\launcher.py"
$webapp = Join-Path $root "server\webapp"

if (-not (Test-Path $py)) { throw "venv python not found at $py - run scripts\install.ps1 first." }

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $py -m pip install --quiet --disable-pip-version-check pyinstaller | Out-Null

# Hidden imports: launcher.py imports app/config/etc. inside functions, and
# botbuilder/botframework are namespace packages PyInstaller needs help finding.
$hidden = @(
    "app", "config", "webchat", "bot", "copilot_runner", "copilot_sessions",
    "session_store", "provisioning", "paths", "devtunnel", "setup", "gui",
    "control", "auth", "version",
    "providers", "providers.base", "providers.registry", "providers.copilot",
    "providers.claude", "providers.openai"
)
$collect = @("botbuilder", "botframework", "aiohttp", "dotenv", "jwt", "cryptography", "msrest", "msal")

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", $mode, "--console",
    "--name", $Name,
    "--paths", (Join-Path $root "server"),
    "--add-data", "$webapp;webapp",
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build\pyinstaller"),
    "--specpath", (Join-Path $root "build")
)

# Bundle the PNG app icon so the desktop Control Panel can display it at runtime
# (resolved via resource_dir()/assets/icon-256.png from _MEIPASS when frozen).
$iconPng = Join-Path $root "assets\icon-256.png"
if (Test-Path $iconPng) {
    $pyiArgs += @("--add-data", "$iconPng;assets")
}
foreach ($h in $hidden)  { $pyiArgs += @("--hidden-import", $h) }
foreach ($c in $collect) { $pyiArgs += @("--collect-all", $c) }

# App icon (embedded in the .exe). Skipped gracefully if the file is missing.
$icon = Join-Path $root "assets\icon.ico"
if (Test-Path $icon) {
    $pyiArgs += @("--icon", $icon)
    Write-Host "Using app icon: $icon" -ForegroundColor Cyan
}

$pyiArgs += $entry

Write-Host "Building $Name ($mode, this can take a few minutes)..." -ForegroundColor Cyan
& $py @pyiArgs

$exe = if ($OneFile) {
    Join-Path $root "dist\$Name.exe"
} else {
    Join-Path $root "dist\$Name\$Name.exe"
}
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`nBuilt: $exe ($mb MB)" -ForegroundColor Green

    # Bundle the Dev Tunnel CLI next to the exe so the package is self-contained
    # (discover_devtunnel checks the exe's own folder). Download it once if the
    # repo's tools/ copy is missing. The setup wizard still handles the case where
    # it's absent at runtime.
    $exeDir   = Split-Path $exe -Parent
    $toolsDt  = Join-Path $root "tools\devtunnel.exe"
    if (-not (Test-Path $toolsDt)) {
        try {
            $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win-arm64" } else { "win-x64" }
            New-Item -ItemType Directory -Force (Split-Path $toolsDt) | Out-Null
            Write-Host "Downloading devtunnel ($arch) to bundle it ..." -ForegroundColor Cyan
            Invoke-WebRequest -Uri "https://aka.ms/TunnelsCliDownload/$arch" -OutFile $toolsDt
        } catch {
            Write-Host "  Could not download devtunnel to bundle (will be offered at first run): $_" -ForegroundColor Yellow
        }
    }
    if (Test-Path $toolsDt) {
        Copy-Item -Force $toolsDt (Join-Path $exeDir "devtunnel.exe")
        Write-Host "Bundled Dev Tunnel CLI next to the exe." -ForegroundColor Green
    }

    Write-Host "Run it:  $exe   (first run shows the setup wizard, then prints the URL + API key)" -ForegroundColor Green
    if (-not $OneFile) {
        Write-Host "Distribute: zip the whole '$Name' folder under dist\." -ForegroundColor Green
    }
}
else {
    throw "Build finished but $exe was not produced. Check the PyInstaller output above."
}

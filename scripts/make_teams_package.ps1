<#
.SYNOPSIS
    Build the Teams app package (zip) for the Copilot bridge bot:
      - generates color.png (192x192) and outline.png (32x32) if missing
      - injects the bot App Id (from .env or -AppId) into a copy of manifest.json
      - zips manifest + icons into teams\copilot-bridge-teams-app.zip
.EXAMPLE
    .\scripts\make_teams_package.ps1
    .\scripts\make_teams_package.ps1 -AppId "<bot-app-id>"
#>
param(
    [string]$AppId = ""
)

$ErrorActionPreference = "Stop"
$root        = Split-Path $PSScriptRoot -Parent
$serverDir   = Join-Path $root "server"
$manifestDir = Join-Path $serverDir "teams\manifest"
$buildDir    = Join-Path $serverDir "teams\build"
$zipPath     = Join-Path $serverDir "teams\copilot-bridge-teams-app.zip"

# --- Resolve the bot App Id --------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($AppId)) {
    $envPath = Join-Path $serverDir ".env"
    if (Test-Path $envPath) {
        $line = Select-String -Path $envPath -Pattern '^MicrosoftAppId=(.+)$' | Select-Object -First 1
        if ($line) { $AppId = $line.Matches[0].Groups[1].Value.Trim() }
    }
}
if ([string]::IsNullOrWhiteSpace($AppId)) {
    throw "No App Id. Pass -AppId <guid> or set MicrosoftAppId in .env (run provision_azure.ps1 first)."
}
Write-Host "Using bot App Id: $AppId" -ForegroundColor Cyan

# --- Generate icons with System.Drawing -------------------------------------------
Add-Type -AssemblyName System.Drawing

function New-ColorIcon($path) {
    $bmp = New-Object System.Drawing.Bitmap 192, 192
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.Clear([System.Drawing.ColorTranslator]::FromHtml("#24292F"))
    $font = New-Object System.Drawing.Font("Consolas", 96, [System.Drawing.FontStyle]::Bold)
    $brush = [System.Drawing.Brushes]::White
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"; $fmt.LineAlignment = "Center"
    $g.DrawString(">_", $font, $brush, (New-Object System.Drawing.RectangleF(0, 0, 192, 192)), $fmt)
    $g.Dispose()
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}

function New-OutlineIcon($path) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.Clear([System.Drawing.Color]::Transparent)
    $font = New-Object System.Drawing.Font("Consolas", 18, [System.Drawing.FontStyle]::Bold)
    $brush = [System.Drawing.Brushes]::White
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"; $fmt.LineAlignment = "Center"
    $g.DrawString(">_", $font, $brush, (New-Object System.Drawing.RectangleF(0, 0, 32, 32)), $fmt)
    $g.Dispose()
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}

New-ColorIcon   (Join-Path $manifestDir "color.png")
New-OutlineIcon (Join-Path $manifestDir "outline.png")
Write-Host "Generated color.png and outline.png" -ForegroundColor Green

# --- Assemble + zip ---------------------------------------------------------------
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
New-Item -ItemType Directory -Path $buildDir | Out-Null

$manifest = Get-Content (Join-Path $manifestDir "manifest.json") -Raw
$manifest = $manifest -replace "__BOT_APP_ID__", $AppId
Set-Content -Path (Join-Path $buildDir "manifest.json") -Value $manifest -Encoding UTF8
Copy-Item (Join-Path $manifestDir "color.png")   (Join-Path $buildDir "color.png")
Copy-Item (Join-Path $manifestDir "outline.png") (Join-Path $buildDir "outline.png")

if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path (Join-Path $buildDir "*") -DestinationPath $zipPath
Write-Host "Built Teams app package: $zipPath" -ForegroundColor Green
Write-Host "Upload it in Teams: Apps -> Manage your apps -> Upload an app -> Upload a custom app." -ForegroundColor Yellow

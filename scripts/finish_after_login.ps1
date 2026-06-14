<#
.SYNOPSIS
    Run this AFTER you finish `az login` with your personal account.
    It verifies your subscription, provisions the Azure Bot (Entra app + secret + bot +
    Teams channel), writes .env, and builds the Teams app package — in one shot.

.EXAMPLE
    az login                          # sign in with zhurongqing@outlook.com (browser)
    .\scripts\finish_after_login.ps1
#>
param(
    [string]$BotName       = "copilot-bridge-bot-$(Get-Random -Maximum 99999)",
    [string]$ResourceGroup = "rg-copilot-bridge",
    [string]$Location      = "westus"
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

# 1. Verify login + subscription -----------------------------------------------------
$acct = az account show 2>$null | ConvertFrom-Json
if (-not $acct) {
    Write-Host "Not logged in yet." -ForegroundColor Yellow
    Write-Host "Run:  az login   (sign in with your personal account in the browser), then re-run this script." -ForegroundColor Yellow
    return
}
Write-Host "Signed in as : $($acct.user.name)"  -ForegroundColor Cyan
Write-Host "Subscription : $($acct.name)"        -ForegroundColor Cyan
Write-Host "Tenant       : $($acct.tenantId)"    -ForegroundColor Cyan

if (-not $acct.id -or $acct.name -eq "N/A(tenant level account)") {
    Write-Host ""
    Write-Host "This account has NO active Azure subscription." -ForegroundColor Yellow
    Write-Host "Create a free one at https://azure.microsoft.com/free (F0 bot is free), then re-run." -ForegroundColor Yellow
    return
}

if ($acct.state -ne "Enabled") {
    Write-Host ""
    Write-Host "Your active subscription '$($acct.name)' is '$($acct.state)', not Enabled." -ForegroundColor Yellow
    Write-Host "Azure cannot create resources on a disabled subscription (even free ones). Fix it by EITHER:" -ForegroundColor Yellow
    Write-Host "  - Re-enable it: https://portal.azure.com -> Subscriptions -> select it -> 'Remove spending limit' / Reactivate" -ForegroundColor Yellow
    Write-Host "  - Or create a fresh free subscription: https://azure.microsoft.com/free" -ForegroundColor Yellow
    Write-Host "If you have another enabled subscription:  az account set -s <name-or-id>" -ForegroundColor Yellow
    Write-Host "Then re-run this script." -ForegroundColor Yellow
    return
}

# Reuse the app registration created on a previous run (avoid duplicate Entra apps).
$existingAppId = ""
$envPath = Join-Path (Split-Path $here -Parent) "server\.env"
if (Test-Path $envPath) {
    $m = Select-String -Path $envPath -Pattern '^MicrosoftAppId=(.+)$' | Select-Object -First 1
    if ($m) { $existingAppId = $m.Matches[0].Groups[1].Value.Trim() }
}

# 2. Provision the Azure Bot (self-healing for tenant policy) ------------------------
$provisionArgs = @{ BotName = $BotName; ResourceGroup = $ResourceGroup; Location = $Location }
if ($existingAppId) {
    Write-Host "Reusing existing app registration from .env: $existingAppId" -ForegroundColor DarkGray
    $provisionArgs.AppId = $existingAppId
}
& (Join-Path $here "provision_azure.ps1") @provisionArgs

# 3. Build the Teams app package -----------------------------------------------------
& (Join-Path $here "make_teams_package.ps1")

# 4. What to do next -----------------------------------------------------------------
Write-Host ""
Write-Host "==================== NEXT STEPS ====================" -ForegroundColor Green
Write-Host "  1. Start the server:   .\scripts\run_server.ps1"
Write-Host "  2. Start the tunnel:   .\scripts\run_tunnel.ps1     (copy the printed https URL)"
Write-Host "  3. Point the bot at it:"
Write-Host "       az bot update -g $ResourceGroup -n $BotName --endpoint `"https://<tunnel>-3978.<region>.devtunnels.ms/api/messages`""
Write-Host "  4. In Teams: Apps -> Manage your apps -> Upload a custom app ->"
Write-Host "       teams\copilot-bridge-teams-app.zip"
Write-Host "  5. Lock it down: message the bot '/whoami', put your id in ALLOWED_USER_IDS in .env, restart."
Write-Host "===================================================" -ForegroundColor Green

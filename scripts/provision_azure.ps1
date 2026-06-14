<#
.SYNOPSIS
    Provision the Azure resources for the Copilot bridge:
      1. An Entra (Azure AD) app registration + client secret  -> the bot identity
      2. A resource group
      3. An Azure Bot resource (F0 / free)  -> with the Teams channel enabled
    Then write the credentials into ..\.env.

.NOTES
    Requires: az CLI, logged in (az login). Uses the current subscription.
    Re-runnable: pass -AppId to reuse an existing app registration instead of creating one.

.EXAMPLE
    .\scripts\provision_azure.ps1
    .\scripts\provision_azure.ps1 -BotName "copilot-bridge-bot-roz" -Endpoint "https://abc-3978.usw2.devtunnels.ms/api/messages"
    .\scripts\provision_azure.ps1 -AppId "<existing-app-id>"   # reuse an app registration
#>
param(
    [string]$BotName       = "copilot-bridge-bot",
    [string]$ResourceGroup = "rg-copilot-bridge",
    [string]$Location      = "westus",
    [ValidateSet("MultiTenant", "SingleTenant")]
    [string]$AppType       = "MultiTenant",
    [string]$DisplayName   = "Copilot CLI Bridge",
    [string]$Endpoint      = "",
    [string]$AppId         = "",
    [string]$Sku           = "F0",
    [switch]$NoWriteEnv
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$serverDir = Join-Path $root "server"   # app server + .env live here after the monorepo split

function Require-Cmd($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { throw "$name not found. $hint" }
}

Require-Cmd az "Install the Azure CLI: https://aka.ms/installazurecli"

# --- Subscription / tenant context -------------------------------------------------
$acct = az account show 2>$null | ConvertFrom-Json
if (-not $acct) { throw "Not logged in. Run 'az login' first." }
$tenantId = $acct.tenantId
Write-Host "Subscription : $($acct.name)" -ForegroundColor Cyan
Write-Host "Tenant       : $tenantId" -ForegroundColor Cyan

if ($acct.state -and $acct.state -ne "Enabled") {
    throw "Active subscription '$($acct.name)' is '$($acct.state)', not Enabled. Re-enable it (portal -> Subscriptions -> Remove spending limit / Reactivate) or 'az account set -s <enabled-sub>', then re-run."
}

# 'az bot' is part of the core CLI in recent versions; only older CLIs need the extension.
az extension add --name botservice --only-show-errors 2>$null | Out-Null

# --- 1. App registration + secret --------------------------------------------------
if ([string]::IsNullOrWhiteSpace($AppId)) {
    $audience = if ($AppType -eq "MultiTenant") { "AzureADMultipleOrgs" } else { "AzureADMyOrg" }
    Write-Host "Creating Entra app registration '$DisplayName' ($audience)..." -ForegroundColor Green
    $errFile = Join-Path $env:TEMP "cb_appcreate.err"
    $app = az ad app create --display-name $DisplayName --sign-in-audience $audience 2>$errFile | ConvertFrom-Json
    if (-not $app.appId -and $AppType -eq "MultiTenant") {
        $createErr = Get-Content $errFile -Raw -ErrorAction SilentlyContinue
        if ($createErr -match "MultipleOrgs|multi-tenant|not allowed as per") {
            Write-Warning "Multi-tenant blocked by policy -> falling back to single-tenant."
            $AppType = "SingleTenant"
            $app = az ad app create --display-name $DisplayName --sign-in-audience AzureADMyOrg | ConvertFrom-Json
        }
    }
    $AppId = $app.appId
    if (-not $AppId) { throw "App registration failed. $((Get-Content $errFile -Raw -ErrorAction SilentlyContinue))" }
    Start-Sleep -Seconds 5   # allow AAD replication
} else {
    Write-Host "Reusing existing app registration: $AppId" -ForegroundColor Yellow
}

# Try a 1-year secret, automatically shortening if the tenant caps credential lifetime.
Write-Host "Creating a client secret..." -ForegroundColor Green
$AppPassword = $null
foreach ($days in @(365, 180, 90, 30, 7)) {
    $endDate = (Get-Date).AddDays($days).ToString("yyyy-MM-ddTHH:mm:ssZ")
    $AppPassword = az ad app credential reset --id $AppId --display-name "copilot-bridge" --end-date $endDate --query password -o tsv 2>$null
    if ($AppPassword) { Write-Host "  secret created (valid ~$days days)" -ForegroundColor DarkGray; break }
}
if (-not $AppPassword) {
    throw "Failed to create a client secret. This tenant likely blocks password secrets entirely (needs a trusted-CA certificate). Use a different subscription/tenant."
}

$tenantArg = @()
if ($AppType -eq "SingleTenant") { $tenantArg = @("--tenant-id", $tenantId) }

# --- 2. Resource group -------------------------------------------------------------
Write-Host "Ensuring resource group '$ResourceGroup' in $Location..." -ForegroundColor Green
az group create --name $ResourceGroup --location $Location 1>$null

# --- 3. Azure Bot resource ---------------------------------------------------------
$endpointArg = @()
if (-not [string]::IsNullOrWhiteSpace($Endpoint)) { $endpointArg = @("--endpoint", $Endpoint) }

Write-Host "Creating Azure Bot '$BotName' (sku $Sku, $AppType)..." -ForegroundColor Green
az bot create `
    --resource-group $ResourceGroup `
    --name $BotName `
    --app-type $AppType `
    --appid $AppId `
    --sku $Sku `
    @tenantArg @endpointArg 1>$null

# --- 4. Enable Microsoft Teams channel --------------------------------------------
Write-Host "Enabling Microsoft Teams channel..." -ForegroundColor Green
az bot msteams create --name $BotName --resource-group $ResourceGroup 1>$null

# --- 5. Write .env -----------------------------------------------------------------
function Set-EnvValue([string]$content, [string]$key, [string]$value) {
    if ($content -match "(?m)^$key=.*$") { return ($content -replace "(?m)^$key=.*$", "$key=$value") }
    return ($content.TrimEnd() + "`n$key=$value`n")
}

if (-not $NoWriteEnv) {
    $envPath = Join-Path $serverDir ".env"
    if (-not (Test-Path $envPath)) { Copy-Item (Join-Path $serverDir ".env.example") $envPath }
    $envText = Get-Content $envPath -Raw
    $envText = Set-EnvValue $envText "MicrosoftAppId" $AppId
    $envText = Set-EnvValue $envText "MicrosoftAppPassword" $AppPassword
    $envText = Set-EnvValue $envText "MicrosoftAppType" $AppType
    $envText = Set-EnvValue $envText "MicrosoftAppTenantId" $(if ($AppType -eq "SingleTenant") { $tenantId } else { "" })
    Set-Content -Path $envPath -Value $envText -Encoding UTF8 -NoNewline
    Write-Host "Wrote credentials to $envPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "==================== PROVISION COMPLETE ====================" -ForegroundColor Cyan
Write-Host "MicrosoftAppId       : $AppId"
Write-Host "MicrosoftAppPassword : (written to .env)"
Write-Host "MicrosoftAppType     : $AppType"
Write-Host "Bot resource         : $BotName  (RG: $ResourceGroup)"
if ($Endpoint) { Write-Host "Messaging endpoint   : $Endpoint" }
else {
    Write-Host "Messaging endpoint   : NOT SET. After you start the tunnel, run:" -ForegroundColor Yellow
    Write-Host "    az bot update -g $ResourceGroup -n $BotName --endpoint `"https://<tunnel>-3978.<region>.devtunnels.ms/api/messages`"" -ForegroundColor White
}
Write-Host "Update the manifest botId/id with: $AppId" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan

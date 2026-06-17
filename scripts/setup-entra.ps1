<#
.SYNOPSIS
    Configure a Microsoft Entra ID app registration for Copilot Bridge and write
    the matching auth settings into the server .env, so the web/mobile clients sign
    in with a Microsoft work account instead of a shared API key.

.DESCRIPTION
    Works entirely through Microsoft Graph using a token obtained from the Azure
    CLI for the TARGET TENANT. Because it never needs an enabled Azure
    subscription, it also works in a tenant whose only subscription is disabled
    (app registrations live in the directory/tenant, not the subscription).

    It will, idempotently:
      1. reuse an existing app (by -AppObjectId or -ClientId or -DisplayName), or
         create a single-tenant one,
      2. set the Application ID URI to api://<appId>,
      3. expose an "access_as_user" delegated scope and force v2 access tokens
         (requestedAccessTokenVersion = 2) so the issuer matches what the server
         validates (login.microsoftonline.com/<tid>/v2.0),
      4. register SPA redirect URIs (http://localhost:<port>/ plus any -PublicUrl),
      5. pre-authorize the app to its own scope (no consent prompt for the owner),
      6. ensure a service principal exists,
      7. write AUTH_MODE=entra + ENTRA_* (incl. the allow-list) into the .env.

    The server stays fail-closed: only the accounts in ENTRA_ALLOWED_USERS may use
    it, even though anyone in the tenant can sign in. By default the allow-list is
    the account you signed in to that tenant with (i.e. only you).

.PARAMETER TenantId
    The directory (tenant) GUID that owns the app. Defaults to the current az tenant.

.PARAMETER ClientId
    Application (client) ID of an existing app registration to configure.

.PARAMETER AppObjectId
    Object ID of an existing app registration (faster/most precise lookup).

.PARAMETER DisplayName
    App registration display name when creating / searching (default "Copilot Bridge").

.PARAMETER Port
    Local server port for the localhost SPA redirect URI (default 3978).

.PARAMETER PublicUrl
    Extra public origins to register as SPA redirect URIs, e.g. your Dev Tunnel URL.

.PARAMETER AllowedUsers
    Who may use the server (emails / UPNs / object ids). Default: the signed-in user.

.PARAMETER EnvPath
    Target .env. Default: %LOCALAPPDATA%\CopilotBridge\.env if it exists (installed
    app), else server\.env (running from source).

.EXAMPLE
    ./scripts/setup-entra.ps1 -TenantId 53f87c5f-245b-41f9-8791-e4a42a2dc058 `
        -ClientId 4b1ffcfa-7e64-4765-84ad-1796ca93ff98
#>
[CmdletBinding()]
param(
    [string]$TenantId,
    [string]$ClientId,
    [string]$AppObjectId,
    [string]$DisplayName = "Copilot Bridge",
    [int]$Port = 3978,
    [string[]]$PublicUrl = @(),
    [string[]]$AllowedUsers = @(),
    [string]$EnvPath
)

$ErrorActionPreference = "Stop"
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Fail "Azure CLI (az) not found. Install it: winget install Microsoft.AzureCLI"
}

# --- resolve tenant + Graph token -----------------------------------------
if (-not $TenantId) {
    $TenantId = az account show --query tenantId -o tsv 2>$null
    if (-not $TenantId) { Fail "Not signed in. Run:  az login --tenant <tenant-guid> --allow-no-subscriptions" }
}
Write-Host "Tenant: $TenantId" -ForegroundColor DarkGray

$token = az account get-access-token --tenant $TenantId --resource https://graph.microsoft.com --query accessToken -o tsv 2>$null
if (-not $token -or $token.Length -lt 100) {
    Fail "Could not get a Graph token for tenant $TenantId. Sign in first:`n  az login --tenant $TenantId --allow-no-subscriptions"
}
$Headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

function Graph {
    param([string]$Method, [string]$Path, $Body)
    $uri = "https://graph.microsoft.com/v1.0$Path"
    $json = $null
    if ($PSBoundParameters.ContainsKey('Body') -and $null -ne $Body) {
        $json = ($Body | ConvertTo-Json -Depth 12)
    }
    try {
        if ($json) {
            return Invoke-RestMethod -Method $Method -Uri $uri -Headers $Headers -Body $json
        } else {
            return Invoke-RestMethod -Method $Method -Uri $uri -Headers $Headers
        }
    } catch {
        $detail = $_.ErrorDetails.Message
        if (-not $detail) { $detail = $_.Exception.Message }
        throw "Graph $Method $Path failed: $detail"
    }
}

# --- resolve the .env ------------------------------------------------------
if (-not $EnvPath) {
    $installed = Join-Path $env:LOCALAPPDATA "CopilotBridge\.env"
    $source    = Join-Path (Split-Path $PSScriptRoot -Parent) "server\.env"
    $EnvPath = if (Test-Path $installed) { $installed } else { $source }
}
Write-Host "Target .env: $EnvPath" -ForegroundColor DarkGray

function Upsert-Env([string]$Key, [string]$Value) {
    $line = "$Key=$Value"
    $dir = Split-Path $EnvPath -Parent
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lines = @()
    if (Test-Path $EnvPath) { $lines = @(Get-Content -LiteralPath $EnvPath) }
    $pattern = "^\s*$([regex]::Escape($Key))\s*="
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern -and $lines[$i] -notmatch '^\s*#') { $lines[$i] = $line; $done = $true; break }
    }
    if (-not $done) {
        if ($lines.Count -and $lines[-1].Trim() -ne "") { $lines += "" }
        $lines += $line
    }
    Set-Content -LiteralPath $EnvPath -Value $lines -Encoding UTF8
}

# --- 1) find or create the app --------------------------------------------
Write-Host "[1/6] Locating the app registration..." -ForegroundColor Cyan
$app = $null
if ($AppObjectId) {
    $app = Graph GET "/applications/$AppObjectId"
} elseif ($ClientId) {
    $app = (Graph GET "/applications?`$filter=appId eq '$ClientId'").value | Select-Object -First 1
} else {
    $app = (Graph GET "/applications?`$filter=displayName eq '$DisplayName'").value | Select-Object -First 1
}
if (-not $app) {
    Write-Host "      Creating a new single-tenant app '$DisplayName'..." -ForegroundColor DarkGray
    $app = Graph POST "/applications" @{ displayName = $DisplayName; signInAudience = "AzureADMyOrg" }
    Start-Sleep -Seconds 5
}
$objId = $app.id
$appId = $app.appId
Write-Host "      App: $($app.displayName)  appId=$appId  objId=$objId" -ForegroundColor Green

# --- 2) identifier URI + 3) scope/v2 + 4) SPA redirects (single PATCH) -----
Write-Host "[2/6] Setting api://$appId, exposing access_as_user (v2 tokens), SPA redirects..." -ForegroundColor Cyan
$scope = $app.api.oauth2PermissionScopes | Where-Object { $_.value -eq "access_as_user" } | Select-Object -First 1
$scopeId = if ($scope) { $scope.id } else { [guid]::NewGuid().ToString() }

$redirects = @("http://localhost:$Port/")
foreach ($u in $PublicUrl) { if ($u) { $redirects += ($u.TrimEnd('/') + '/') } }
# Keep any redirect URIs already configured, then de-dup.
if ($app.spa -and $app.spa.redirectUris) { $redirects += $app.spa.redirectUris }
$redirects = $redirects | Where-Object { $_ } | Select-Object -Unique

# Step A: identifier URI + the scope + v2 tokens + SPA redirects. (Graph validates
# preAuthorizedApplications against EXISTING scopes, so that must be a second PATCH.)
$patch = [ordered]@{
    identifierUris = @("api://$appId")
    spa = @{ redirectUris = @($redirects) }
    api = [ordered]@{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(
            [ordered]@{
                id = $scopeId
                adminConsentDescription = "Allow Copilot Bridge to act as the signed-in user."
                adminConsentDisplayName = "Access Copilot Bridge"
                userConsentDescription  = "Allow Copilot Bridge to act on your behalf."
                userConsentDisplayName   = "Access Copilot Bridge"
                value = "access_as_user"
                type = "User"
                isEnabled = $true
            }
        )
    }
}
Graph PATCH "/applications/$objId" $patch | Out-Null

# Step B: now that the scope exists, pre-authorize the app to its own scope so the
# owner never sees a consent prompt. Best-effort (don't fail the whole run on this).
try {
    $patchB = @{ api = @{ preAuthorizedApplications = @(
        [ordered]@{ appId = $appId; delegatedPermissionIds = @($scopeId) }
    ) } }
    Graph PATCH "/applications/$objId" $patchB | Out-Null
} catch {
    Write-Host "      (pre-authorize step skipped: $($_.Exception.Message))" -ForegroundColor Yellow
}
Write-Host "      Redirect URIs: $($redirects -join ', ')" -ForegroundColor Green

# --- 5) service principal --------------------------------------------------
Write-Host "[3/6] Ensuring a service principal exists..." -ForegroundColor Cyan
$sp = (Graph GET "/servicePrincipals?`$filter=appId eq '$appId'").value | Select-Object -First 1
if (-not $sp) {
    $sp = Graph POST "/servicePrincipals" @{ appId = $appId }
    Write-Host "      Created service principal $($sp.id)" -ForegroundColor Green
} else {
    Write-Host "      Service principal already present" -ForegroundColor DarkGray
}

# --- allow-list defaults to the signed-in user ----------------------------
Write-Host "[4/6] Resolving the allow-list..." -ForegroundColor Cyan
if (-not $AllowedUsers -or $AllowedUsers.Count -eq 0) {
    try {
        $me = Graph GET "/me"
        if ($me.userPrincipalName) { $AllowedUsers = @($me.userPrincipalName) }
    } catch {
        Write-Host "      (couldn't read /me; set -AllowedUsers explicitly)" -ForegroundColor Yellow
    }
}

# --- 5) write .env ---------------------------------------------------------
Write-Host "[5/6] Writing auth settings to .env ..." -ForegroundColor Cyan
Upsert-Env "AUTH_MODE"           "entra"
Upsert-Env "ENTRA_TENANT_ID"     $TenantId
Upsert-Env "ENTRA_CLIENT_ID"     $appId
Upsert-Env "ENTRA_AUDIENCE"      "api://$appId,$appId"
Upsert-Env "ENTRA_SCOPES"        "api://$appId/access_as_user"
Upsert-Env "ENTRA_ALLOWED_USERS" ($AllowedUsers -join ",")

# --- 6) summary ------------------------------------------------------------
Write-Host "[6/6] Done." -ForegroundColor Cyan
Write-Host ""
Write-Host "Microsoft sign-in is configured:" -ForegroundColor Green
Write-Host "  Tenant        : $TenantId"
Write-Host "  Client (app)  : $appId"
Write-Host "  Audience      : api://$appId  (and $appId)"
Write-Host "  Scope         : api://$appId/access_as_user"
Write-Host "  Allowed users : $($AllowedUsers -join ', ')"
Write-Host "  Redirect URIs : $($redirects -join ', ')"
Write-Host ""
Write-Host "Next:" -ForegroundColor Yellow
Write-Host "  - Restart the Copilot Bridge server (Control Panel -> Restart, or relaunch)."
Write-Host "  - Open the web client; click the gear and 'Sign in with Microsoft'."
Write-Host "  - For phone access over the Dev Tunnel, re-run with -PublicUrl <devtunnels URL>"
Write-Host "    so its origin is a registered SPA redirect URI."

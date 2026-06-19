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
    [string]$EnvPath,
    # --- team automation ---
    [switch]$ShareConfig,       # admin: print a share code (tenant/client/audience/scopes)
    [string]$FromConfig,        # colleague: hydrate .env from a share code (no Azure needed)
    [string[]]$InviteGuest = @(),  # admin: invite these emails as B2B guests (+ -PublicUrl redirect)
    [switch]$RequestAccess,     # colleague: print an access-request code + email to the admin
    [string]$AdminEmail,        # colleague: who to email the request to
    [string]$ApproveRequest     # admin: approve a colleague's request code (invite + redirect)
)

$ErrorActionPreference = "Stop"
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# --- share-code helpers (pack/unpack the NON-secret sign-in config) ---------
function New-ShareCode([string]$Tenant, [string]$Client, [string]$Audience, [string]$Scopes) {
    if (-not $Tenant -or -not $Client) { return "" }
    $payload = [ordered]@{ v = 1; t = $Tenant; c = $Client; a = $Audience; s = $Scopes }
    $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress)))
    return "CBCFG1." + ($b64.TrimEnd('=').Replace('+', '-').Replace('/', '_'))
}
function Read-ShareCode([string]$Code) {
    if (-not $Code -or -not $Code.StartsWith("CBCFG1.")) { Fail "Not a Copilot Bridge sign-in code (must start with CBCFG1.)." }
    $b64 = $Code.Substring(7).Replace('-', '+').Replace('_', '/')
    switch ($b64.Length % 4) { 2 { $b64 += '==' } 3 { $b64 += '=' } 1 { Fail "Corrupt share code." } }
    try { return ([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) | ConvertFrom-Json) }
    catch { Fail "Corrupt share code: $($_.Exception.Message)" }
}
function New-RequestCode([string]$Account, [string]$PublicUrl, [string]$Tenant, [string[]]$Users = @()) {
    if (-not $Account) { return "" }
    $url = if ($PublicUrl) { $PublicUrl.TrimEnd('/') + '/' } else { "" }
    $payload = [ordered]@{ v = 1; u = $Account; r = $url; t = $Tenant }
    $list = @($Users | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    if ($list.Count -gt 0) { $payload['al'] = ($list -join ',') }
    $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress)))
    return "CBREQ1." + ($b64.TrimEnd('=').Replace('+', '-').Replace('/', '_'))
}
function Read-RequestCode([string]$Code) {
    if (-not $Code -or -not $Code.StartsWith("CBREQ1.")) { Fail "Not a Copilot Bridge access-request code (must start with CBREQ1.)." }
    $b64 = $Code.Substring(7).Replace('-', '+').Replace('_', '/')
    switch ($b64.Length % 4) { 2 { $b64 += '==' } 3 { $b64 += '=' } 1 { Fail "Corrupt request code." } }
    try { return ([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) | ConvertFrom-Json) }
    catch { Fail "Corrupt request code: $($_.Exception.Message)" }
}
function Read-EnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path $Path)) { return "" }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=" -and $line -notmatch '^\s*#') {
            return ($line -split '=', 2)[1].Trim()
        }
    }
    return ""
}

# --- resolve the .env (needed by several modes) ----------------------------
if (-not $EnvPath) {
    $installed = Join-Path $env:LOCALAPPDATA "CopilotBridge\.env"
    $source    = Join-Path (Split-Path $PSScriptRoot -Parent) "server\.env"
    $EnvPath = if (Test-Path $installed) { $installed } else { $source }
}

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

# === MODE: -ShareConfig (admin prints a code; no Azure needed) ==============
if ($ShareConfig) {
    $t = if ($TenantId) { $TenantId } else { Read-EnvValue $EnvPath "ENTRA_TENANT_ID" }
    $c = if ($ClientId) { $ClientId } else { Read-EnvValue $EnvPath "ENTRA_CLIENT_ID" }
    $a = Read-EnvValue $EnvPath "ENTRA_AUDIENCE"; if (-not $a -and $c) { $a = "api://$c,$c" }
    $s = Read-EnvValue $EnvPath "ENTRA_SCOPES";   if (-not $s -and $c) { $s = "api://$c/access_as_user" }
    if (-not $t -or -not $c) { Fail "No tenant/client found. Pass -TenantId/-ClientId or run this where .env is configured." }
    $code = New-ShareCode $t $c $a $s
    Write-Host ""
    Write-Host "Share this code with your colleague (safe to send - no key/secret/allow-list):" -ForegroundColor Green
    Write-Host $code -ForegroundColor White
    Write-Host ""
    Write-Host "They run:  .\scripts\setup-entra.ps1 -FromConfig $code -AllowedUsers <their-account>" -ForegroundColor Yellow
    exit 0
}

# === MODE: -FromConfig (colleague hydrates .env; no Azure needed) ===========
if ($FromConfig) {
    $o = Read-ShareCode $FromConfig
    $t = "$($o.t)"; $c = "$($o.c)"
    $a = if ($o.a) { "$($o.a)" } else { "api://$c,$c" }
    $s = if ($o.s) { "$($o.s)" } else { "api://$c/access_as_user" }
    if (-not $AllowedUsers -or $AllowedUsers.Count -eq 0) {
        $me = (whoami /upn 2>$null)
        if ($me) { $AllowedUsers = @($me.Trim()) }
    }
    Upsert-Env "AUTH_MODE"           "entra"
    Upsert-Env "ENTRA_TENANT_ID"     $t
    Upsert-Env "ENTRA_CLIENT_ID"     $c
    Upsert-Env "ENTRA_AUDIENCE"      $a
    Upsert-Env "ENTRA_SCOPES"        $s
    Upsert-Env "ENTRA_ALLOWED_USERS" ($AllowedUsers -join ",")
    Write-Host "Imported sign-in config into $EnvPath" -ForegroundColor Green
    Write-Host "  Tenant        : $t"
    Write-Host "  Client (app)  : $c"
    Write-Host "  Allowed users : $($AllowedUsers -join ', ')"
    Write-Host ""
    Write-Host "Next:" -ForegroundColor Yellow
    Write-Host "  1) Restart the Copilot Bridge server (Control Panel -> Restart)."
    Write-Host "  2) Send the app owner your Dev Tunnel URL (Control Panel -> Server URL) and"
    Write-Host "     your account so they can invite you as a guest + register your redirect URI."
    exit 0
}

# === MODE: -RequestAccess (colleague emails the admin a request; no Azure) ==
if ($RequestAccess) {
    $account = if ($AllowedUsers -and $AllowedUsers.Count -gt 0) { "$($AllowedUsers[0])".Trim() }
               else { (whoami /upn 2>$null) }
    if ($account) { $account = "$account".Trim() }
    if (-not $account) { Fail "Couldn't determine your account. Pass -AllowedUsers <your-email>." }
    $tenant = if ($TenantId) { $TenantId } else { Read-EnvValue $EnvPath "ENTRA_TENANT_ID" }
    $admin  = if ($AdminEmail) { $AdminEmail } else { Read-EnvValue $EnvPath "ENTRA_ADMIN_CONTACT" }
    # Best-effort: this host's Dev Tunnel URL from connection.json.
    $redirect = ""
    foreach ($cj in @((Join-Path (Split-Path $EnvPath -Parent) "connection.json"))) {
        if (Test-Path $cj) {
            try { $redirect = (Get-Content $cj -Raw | ConvertFrom-Json).publicUrl } catch {}
        }
    }
    foreach ($u in $PublicUrl) { if ($u) { $redirect = $u } }  # explicit -PublicUrl wins
    $rcode = New-RequestCode $account $redirect $tenant $AllowedUsers
    Write-Host ""
    Write-Host "Send this access-request code to your admin ($([string]::IsNullOrWhiteSpace($admin) ? '<admin email unknown>' : $admin)):" -ForegroundColor Green
    Write-Host $rcode -ForegroundColor White
    Write-Host ""
    Write-Host "Your account     : $account"
    Write-Host "Your redirect URI: $([string]::IsNullOrWhiteSpace($redirect) ? '(start the server first to create it)' : $redirect)"
    Write-Host ""
    Write-Host "The admin approves with:" -ForegroundColor Yellow
    Write-Host "  .\scripts\setup-entra.ps1 -ApproveRequest $rcode"
    exit 0
}

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

Write-Host "Target .env: $EnvPath" -ForegroundColor DarkGray

# === MODE: -ApproveRequest (admin approves a colleague's request code) =======
if ($ApproveRequest) {
    $req = Read-RequestCode $ApproveRequest
    $email = "$($req.u)".Trim()
    $url   = "$($req.r)".Trim()
    if (-not $email) { Fail "Request code has no account." }
    # Every account the colleague asked to allow-list (falls back to the requester).
    $emails = @()
    if ($req.al) { $emails = @("$($req.al)".Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    if (-not $emails -or $emails.Count -eq 0) { $emails = @($email) }
    # Invite each as a B2B guest.
    foreach ($em in $emails) {
        try {
            $inv = Graph POST "/invitations" @{
                invitedUserEmailAddress = $em
                inviteRedirectUrl       = "https://myapplications.microsoft.com"
                sendInvitationMessage   = $true
            }
            Write-Host "Invited guest: $em  (status: $($inv.status))" -ForegroundColor Green
        } catch {
            Write-Host "Invite note for ${em}: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    # Register their redirect URI on the shared app.
    if ($url) {
        $app = if ($AppObjectId) { Graph GET "/applications/$AppObjectId" }
               elseif ($ClientId) { (Graph GET "/applications?`$filter=appId eq '$ClientId'").value | Select-Object -First 1 }
               else { (Graph GET "/applications?`$filter=displayName eq '$DisplayName'").value | Select-Object -First 1 }
        if (-not $app) { Fail "App not found. Pass -ClientId (or -AppObjectId)." }
        $r = @()
        if ($app.spa -and $app.spa.redirectUris) { $r += $app.spa.redirectUris }
        $r += ($url.TrimEnd('/') + '/')
        $r = $r | Where-Object { $_ } | Select-Object -Unique
        Graph PATCH "/applications/$($app.id)" @{ spa = @{ redirectUris = @($r) } } | Out-Null
        Write-Host "Registered redirect URI: $url" -ForegroundColor Green
        Write-Host "Redirect URIs now: $($r -join ', ')" -ForegroundColor DarkGray
    } else {
        Write-Host "No redirect URI in the request (colleague hadn't started their server). Re-run when they have one." -ForegroundColor Yellow
    }
    Write-Host "Done. $($emails -join ', ') accept the email invite, then sign in on their machine." -ForegroundColor Cyan
    exit 0
}

# === MODE: -InviteGuest (admin invites B2B guests + registers their redirect) ==
if ($InviteGuest -and $InviteGuest.Count -gt 0) {
    foreach ($email in $InviteGuest) {
        $email = "$email".Trim()
        if (-not $email) { continue }
        try {
            $inv = Graph POST "/invitations" @{
                invitedUserEmailAddress = $email
                inviteRedirectUrl       = "https://myapplications.microsoft.com"
                sendInvitationMessage   = $true
            }
            Write-Host "Invited guest: $email  (status: $($inv.status))" -ForegroundColor Green
        } catch {
            Write-Host "Invite failed for ${email}: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    # Register the colleague's tunnel URL(s) as SPA redirect URIs on the app.
    if ($PublicUrl -and $PublicUrl.Count -gt 0) {
        if (-not ($ClientId -or $AppObjectId)) { Fail "Adding a redirect URI needs -ClientId (or -AppObjectId)." }
        $app = if ($AppObjectId) { Graph GET "/applications/$AppObjectId" }
               else { (Graph GET "/applications?`$filter=appId eq '$ClientId'").value | Select-Object -First 1 }
        if (-not $app) { Fail "App not found for the given -ClientId/-AppObjectId." }
        $r = @()
        if ($app.spa -and $app.spa.redirectUris) { $r += $app.spa.redirectUris }
        foreach ($u in $PublicUrl) { if ($u) { $r += ($u.TrimEnd('/') + '/') } }
        $r = $r | Where-Object { $_ } | Select-Object -Unique
        Graph PATCH "/applications/$($app.id)" @{ spa = @{ redirectUris = @($r) } } | Out-Null
        Write-Host "Redirect URIs now: $($r -join ', ')" -ForegroundColor Green
    }
    Write-Host "Done. The guest accepts the email invite, then signs in on their machine." -ForegroundColor Cyan
    exit 0
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

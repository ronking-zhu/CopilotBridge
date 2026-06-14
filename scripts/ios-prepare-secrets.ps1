<#
.SYNOPSIS
    Base64-encodes the three Apple signing assets and stores every secret the
    iOS TestFlight workflow needs as GitHub repo secrets (via the `gh` CLI).

.DESCRIPTION
    The .github/workflows/ios-testflight.yml pipeline builds and ships the iOS
    client to TestFlight from a macOS cloud runner. It expects these repo
    secrets, which this script creates for you:

      IOS_DIST_CERT_BASE64          base64 of your Apple Distribution .p12
      IOS_DIST_CERT_PASSWORD        password used when exporting that .p12
      IOS_PROVISION_PROFILE_BASE64  base64 of the App Store .mobileprovision
      IOS_KEYCHAIN_PASSWORD         throwaway temp-keychain password (auto-generated)
      APPSTORE_API_KEY_BASE64       base64 of the App Store Connect AuthKey_XXX.p8
      APPSTORE_KEY_ID               the API key's Key ID
      APPSTORE_ISSUER_ID            the API key's Issuer ID

    Get the assets first (see clients/ios/README.md):
      * .p12  – export your "Apple Distribution" certificate + private key from
                Keychain Access (or create one in the Apple Developer portal).
      * .mobileprovision – an App Store provisioning profile for
                com.copilotbridge.client.
      * AuthKey_XXX.p8 – an App Store Connect API key (Users and Access → Integrations
                → App Store Connect API), plus its Key ID and Issuer ID.

    Requires the GitHub CLI (https://cli.github.com) authenticated with `gh auth login`.

.EXAMPLE
    ./scripts/ios-prepare-secrets.ps1 `
        -DistCertP12 C:\keys\dist.p12 -DistCertPassword 'p12pass' `
        -ProvisioningProfile C:\keys\AppStore.mobileprovision `
        -AscApiKeyP8 C:\keys\AuthKey_ABC123.p8 -AscKeyId ABC123 -AscIssuerId 11111111-2222-3333-4444-555555555555

.EXAMPLE
    # Just print the base64 values instead of setting secrets:
    ./scripts/ios-prepare-secrets.ps1 -DistCertP12 ... -PrintOnly
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$DistCertP12,
    [Parameter(Mandatory)] [string]$DistCertPassword,
    [Parameter(Mandatory)] [string]$ProvisioningProfile,
    [Parameter(Mandatory)] [string]$AscApiKeyP8,
    [Parameter(Mandatory)] [string]$AscKeyId,
    [Parameter(Mandatory)] [string]$AscIssuerId,
    [string]$Repo = "roz_microsoft/copilot-bridge",
    [string]$KeychainPassword = ([guid]::NewGuid().ToString("N")),
    [switch]$PrintOnly
)

$ErrorActionPreference = "Stop"

function Get-Base64File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "File not found: $Path" }
    return [Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path)))
}

$certB64    = Get-Base64File $DistCertP12
$profileB64 = Get-Base64File $ProvisioningProfile
$keyB64     = Get-Base64File $AscApiKeyP8

$secrets = [ordered]@{
    IOS_DIST_CERT_BASE64         = $certB64
    IOS_DIST_CERT_PASSWORD       = $DistCertPassword
    IOS_PROVISION_PROFILE_BASE64 = $profileB64
    IOS_KEYCHAIN_PASSWORD        = $KeychainPassword
    APPSTORE_API_KEY_BASE64      = $keyB64
    APPSTORE_KEY_ID              = $AscKeyId
    APPSTORE_ISSUER_ID           = $AscIssuerId
}

if ($PrintOnly) {
    Write-Host "Add these as GitHub repo secrets ($Repo):`n" -ForegroundColor Cyan
    foreach ($k in $secrets.Keys) {
        # Truncate long base64 blobs for readability when printing.
        $v = $secrets[$k]
        $preview = if ($v.Length -gt 60) { $v.Substring(0, 60) + "… (" + $v.Length + " chars)" } else { $v }
        Write-Host ("{0,-30} = {1}" -f $k, $preview)
    }
    return
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) not found. Install from https://cli.github.com and run 'gh auth login', or re-run with -PrintOnly."
}

Write-Host "Setting secrets on $Repo ..." -ForegroundColor Cyan
foreach ($k in $secrets.Keys) {
    # Pipe the value via stdin (gh reads the body from stdin when --body is
    # omitted) so it never appears in the process command line.
    $secrets[$k] | gh secret set $k --repo $Repo
    if ($LASTEXITCODE -ne 0) { throw "Failed to set secret $k" }
    Write-Host "  set $k" -ForegroundColor Green
}

Write-Host "`nDone. Trigger the build: Actions -> iOS TestFlight -> Run workflow (or push a tag 'ios-v*')." -ForegroundColor Cyan

<#
.SYNOPSIS
    Expose the local bridge server on a public HTTPS URL using Microsoft Dev Tunnels.
    The printed https URL + "/api/messages" is what you set as the Azure Bot messaging endpoint.
.EXAMPLE
    .\scripts\run_tunnel.ps1                 # persistent tunnel "copilot-bridge" on port 3978
    .\scripts\run_tunnel.ps1 -Port 3978
    .\scripts\run_tunnel.ps1 -Ephemeral      # quick temporary tunnel (URL changes each run)
#>
param(
    [int]$Port = 3978,
    [string]$TunnelId = "copilot-bridge",
    [switch]$Ephemeral
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command devtunnel -ErrorAction SilentlyContinue)) {
    throw "devtunnel not found. Install it with:  winget install Microsoft.devtunnel"
}

Write-Host "Signing in to Dev Tunnels (a browser/device-code prompt may appear)..." -ForegroundColor Cyan
devtunnel user login | Out-Host

if ($Ephemeral) {
    Write-Host "Hosting an ephemeral anonymous tunnel for port $Port ..." -ForegroundColor Green
    Write-Host "Use the printed https URL + /api/messages as the bot endpoint." -ForegroundColor Yellow
    devtunnel host -p $Port --allow-anonymous
    return
}

# Persistent, named tunnel so the URL stays stable across restarts.
# NOTE: --protocol is the protocol the *local* service speaks. The bridge server
# (app.py) serves plain HTTP, so this must be 'http'. Using 'https' makes devtunnel
# attempt a TLS handshake against the HTTP port -> 502 Bad Gateway. The public
# tunnel URL is always HTTPS regardless (the relay terminates TLS).
devtunnel create $TunnelId --allow-anonymous 2>$null | Out-Null
devtunnel port create $TunnelId -p $Port --protocol http 2>$null | Out-Null

$show = devtunnel show $TunnelId 2>$null | Out-String
if ($show -match "(https://[A-Za-z0-9\-]+\.[A-Za-z0-9\-]+\.devtunnels\.ms)") {
    $base = $Matches[1]
    Write-Host ""
    Write-Host "Tunnel base URL : $base" -ForegroundColor Green
    Write-Host "Messaging endpoint (set this on the Azure Bot):" -ForegroundColor Yellow
    Write-Host "    $base/api/messages" -ForegroundColor White
    Write-Host ""
}

Write-Host "Hosting tunnel '$TunnelId' for http://localhost:$Port  (Ctrl+C to stop)..." -ForegroundColor Green
devtunnel host $TunnelId

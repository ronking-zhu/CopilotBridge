<#
.SYNOPSIS
    Publish the Windows WPF client as a self-contained, single-file .exe.
    win-x64 is the primary target; win-arm64 is supported for newer ARM devices.
.EXAMPLE
    .\scripts\publish-client.ps1
    .\scripts\publish-client.ps1 -Runtime win-arm64
#>
param(
    [ValidateSet("win-x64", "win-arm64")]
    [string]$Runtime = "win-x64"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$proj = Join-Path $root "clients\windows\CopilotBridgeClient.csproj"
$out  = Join-Path $root "clients\windows\publish\$Runtime"

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw "dotnet SDK not found. Install the .NET 9 SDK: https://dotnet.microsoft.com/download"
}

Write-Host "Publishing Windows client for $Runtime ..." -ForegroundColor Cyan
dotnet publish $proj -c Release -r $Runtime -p:PublishSingleFile=true -o $out

$exe = Join-Path $out "CopilotBridgeClient.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "Published: $exe ($mb MB)" -ForegroundColor Green
}

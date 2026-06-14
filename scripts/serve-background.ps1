<#
.SYNOPSIS
    Run the Copilot Bridge (app server + Dev Tunnel) in the background, fully
    INDEPENDENT of VS Code. It keeps running after you close the editor and
    auto-starts every time you log in to Windows. No admin rights needed.

.DESCRIPTION
    Processes started from a VS Code terminal are children of that terminal and
    die when VS Code closes. This script instead launches the launcher through
    WMI (Win32_Process.Create), so the new process is parented to the system WMI
    host - not VS Code - and survives the editor closing. For auto-start at
    logon it drops a hidden shortcut in your Startup folder (no Task Scheduler /
    admin required). All output is captured to logs\launcher.log.

.PARAMETER Action
    install   (default) add the logon Startup shortcut AND start it now
    start     start it now, detached (no shortcut changes)
    stop      stop the server + Dev Tunnel
    status    show server/tunnel state, health and the public URL
    uninstall remove the Startup shortcut and stop it

.EXAMPLE
    .\scripts\serve-background.ps1            # start now + auto-start at logon
    .\scripts\serve-background.ps1 status
    .\scripts\serve-background.ps1 stop
    .\scripts\serve-background.ps1 uninstall
#>
param(
    [ValidateSet("install", "start", "stop", "status", "uninstall")]
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"
$root     = Split-Path $PSScriptRoot -Parent
$py       = Join-Path $root ".venv\Scripts\python.exe"
$launcher = Join-Path $root "server\launcher.py"
$log      = Join-Path $root "logs\launcher.log"
$startup  = [Environment]::GetFolderPath('Startup')
$lnk      = Join-Path $startup "CopilotBridge.lnk"

function Get-LaunchCommandLine {
    # Hidden PowerShell that runs the launcher and captures stdout + logging
    # (Python logging writes to stderr, so 2>&1 is required) to the log file.
    # CB_NO_GUI=1 suppresses the desktop login/connection dialogs for this
    # headless background service (it has no attached desktop session).
    $inner = "`$env:CB_NO_GUI='1'; & '$py' '$launcher' 2>&1 | Out-File -FilePath '$log' -Encoding utf8"
    return "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$inner`""
}

function Stop-StrayProcesses {
    # Only our tunnel (host copilot-bridge) - never touch other projects' devtunnels.
    Get-CimInstance Win32_Process -Filter "Name = 'devtunnel.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'copilot-bridge' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match 'launcher\.py' -or $_.CommandLine -match 'server\\app\.py') } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Start-Detached {
    if (-not (Test-Path $py)) { throw "venv python not found at $py - run scripts\install.ps1 first." }
    New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
    Stop-StrayProcesses
    Start-Sleep 1
    # Win32_Process.Create spawns a process owned by the WMI host, detached from
    # this shell and from VS Code, so it survives the editor closing.
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = (Get-LaunchCommandLine)
        CurrentDirectory = $root
    }
    if ($res.ReturnValue -ne 0) { throw "Win32_Process.Create failed (code $($res.ReturnValue))." }
    Write-Host "Launched detached (pid $($res.ProcessId)), independent of VS Code." -ForegroundColor Green
}

function Install-StartupShortcut {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath       = "powershell.exe"
    $sc.Arguments        = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" start"
    $sc.WorkingDirectory = $root
    $sc.WindowStyle      = 7   # minimized
    $sc.Description       = "Start Copilot Bridge (app server + Dev Tunnel) at logon"
    $sc.Save()
    Write-Host "Added logon auto-start shortcut: $lnk" -ForegroundColor Green
}

function Wait-Healthy {
    Write-Host "Waiting for the server to become ready..."
    for ($i = 0; $i -lt 50; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:3978/health" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        }
        catch { }
    }
    return $false
}

function Show-Status {
    if (Test-Path $lnk) { Write-Host "Logon auto-start: ENABLED ($lnk)" -ForegroundColor Cyan }
    else { Write-Host "Logon auto-start: disabled" -ForegroundColor Yellow }
    $listen = (Get-NetTCPConnection -LocalPort 3978 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
    $tunnel = (Get-CimInstance Win32_Process -Filter "Name='devtunnel.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'copilot-bridge' } | Measure-Object).Count
    Write-Host ("Server :3978 listening = {0} | copilot-bridge devtunnel procs = {1}" -f $listen, $tunnel)
    try {
        $h = Invoke-RestMethod -Uri "http://localhost:3978/health" -TimeoutSec 3
        Write-Host ("Health OK: authMode={0}, scope={1}" -f $h.authMode, $h.scope) -ForegroundColor Green
        $card = Join-Path $env:LOCALAPPDATA "CopilotBridge\connection.json"
        if (Test-Path $card) {
            $pub = (Get-Content $card -Raw | ConvertFrom-Json).publicUrl
            if ($pub) { Write-Host "Public URL: $pub" }
        }
    }
    catch {
        Write-Host "Health: not responding" -ForegroundColor Yellow
    }
}

switch ($Action) {
    "status" { Show-Status }

    "stop" {
        Stop-StrayProcesses
        Start-Sleep 1
        Write-Host "Stopped." -ForegroundColor Green
        Show-Status
    }

    "uninstall" {
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed logon shortcut." -ForegroundColor Green }
        else { Write-Host "No logon shortcut to remove." }
        Stop-StrayProcesses
        Write-Host "Stopped and disabled auto-start." -ForegroundColor Green
    }

    "start" {
        Start-Detached
        if (Wait-Healthy) { Write-Host "Copilot Bridge is READY." -ForegroundColor Green }
        else { Write-Host "Started, but /health did not respond in time. Check the log: $log" -ForegroundColor Yellow }
        Show-Status
    }

    default {
        # install
        Install-StartupShortcut
        Start-Detached
        if (Wait-Healthy) {
            Write-Host "Copilot Bridge is READY and runs independently of VS Code." -ForegroundColor Green
        }
        else {
            Write-Host "Started, but /health did not respond in time. Check the log: $log" -ForegroundColor Yellow
        }
        Show-Status
        Write-Host "`nControl it any time with:" -ForegroundColor DarkGray
        Write-Host "  .\scripts\serve-background.ps1 status | stop | uninstall" -ForegroundColor DarkGray
    }
}

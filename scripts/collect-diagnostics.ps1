<#
.SYNOPSIS
Collect read-only Copilot Bridge startup/authentication diagnostics without Python.
.DESCRIPTION
No HTTP requests, account sign-in, service control, process termination, database
access, or configuration edits. Writes a sanitized JSON report and ZIP only.
Process-scope environment belongs to THIS collector, not the running server.
Raw .env, connection cards, command lines, and chat/model output are not exported.
.PARAMETER DataDirectory
Installed application data; defaults to the current user's LocalAppData.
.PARAMETER Port
Port shown in Control Panel. Used only to inspect OS listener metadata.
.EXAMPLE
& .\collect-diagnostics.ps1 -Port 3978
.EXAMPLE
& .\collect-diagnostics.ps1 -SelfTest
#>
[CmdletBinding()]
param(
    [string]$DataDirectory = (Join-Path $env:LOCALAPPDATA 'CopilotBridge'),
    [string]$OutputDirectory = (Join-Path $env:USERPROFILE 'Downloads'),
    [ValidateRange(1, 65535)][int]$Port = 3978,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'

function Protect-Text([AllowNull()][string]$Text) {
    if (-not $Text) { return '' }
    $Text = $Text -replace '(?i)Bearer\s+\S+', 'Bearer <redacted>'
    $Text = $Text -replace '(?i)((?:CHAT_API_TOKEN|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|client[_ -]?secret|password|authorization|x-api-key)\s*[:=]\s*)(?:"[^"]*"|''[^'']*''|\S+)', '${1}<redacted>'
    $Text = $Text -replace '(?i)https?://[^\s"<>]+', '<url>'
    $Text = $Text -replace '[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '<account>'
    $Text = $Text -replace '(?i)[A-Z]:[\\/]Users[\\/][^\\/\s"'']+', '%USERPROFILE%'
    $Text = $Text -replace '(?<![\w-])[A-Za-z0-9_-]{32,}(?![\w-])', '<long-value>'
    return $Text
}

function Read-EnvValues([string]$Path) {
    $values = @{}
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
            if ($line -match '^\s*(?:export\s+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
                $key = $Matches.key
                $value = $Matches.value.Trim()
                if ($value -match '^"([^"\r\n]*)"\s*(?:#.*)?$' -or $value -match '^''([^''\r\n]*)''\s*(?:#.*)?$') {
                    $value = $Matches[1]
                } else {
                    $value = ($value -replace '\s+#.*$', '').Trim()
                }
                $values[$key] = $value
            }
        }
    }
    return $values
}

function Select-SafeSettings($Values) {
    $safe = [ordered]@{}
    foreach ($name in @('AUTH_MODE', 'HOST', 'PORT', 'TUNNEL_AUTH', 'TUNNEL_ENABLED', 'ONEDRIVE_SYNC_ENABLED', 'SESSION_WATCHER_ENABLED', 'CB_NO_GUI')) {
        if (-not $Values.ContainsKey($name)) { $safe[$name] = '<unset>'; continue }
        $value = [string]$Values[$name]
        if ($value -eq '') { $safe[$name] = '<empty>'; continue }
        $pattern = switch ($name) {
            'AUTH_MODE' { '^(tunnel|entra|both|apikey)$' }
            'HOST' { '^(localhost|127\.0\.0\.1|::1|0\.0\.0\.0|::)$' }
            'PORT' { '^\d{1,5}$' }
            'TUNNEL_AUTH' { '^(private|tenant|anonymous)$' }
            default { '^(true|false|1|0|yes|no|on|off)$' }
        }
        $safe[$name] = if ($value -match $pattern) { $value } else { '<set: nonstandard value omitted>' }
    }
    $safe['ControlKeyPresent'] = [bool]$Values['CHAT_API_TOKEN']
    foreach ($name in @('ENTRA_TENANT_ID', 'ENTRA_CLIENT_ID', 'ENTRA_AUDIENCE', 'ENTRA_ALLOWED_USERS', 'ENTRA_ALLOWED_GROUPS', 'ENTRA_ALLOWED_ROLES', 'ONEDRIVE_CLIENT_ID')) {
        $safe["${name}_Present"] = [bool]$Values[$name]
    }
    return $safe
}

function Get-FileConfig([string]$Directory) {
    $path = Join-Path $Directory '.env'
    $values = Read-EnvValues $path
    $result = [ordered]@{ Directory = Protect-Text $Directory; Exists = (Test-Path -LiteralPath $path -PathType Leaf); Settings = Select-SafeSettings $values }
    if ($result.Exists) { $result['LastWriteUtc'] = (Get-Item -LiteralPath $path).LastWriteTimeUtc.ToString('o') }
    $cardPath = Join-Path $Directory 'connection.json'
    $result['ConnectionCardExists'] = Test-Path -LiteralPath $cardPath -PathType Leaf
    if ($result.ConnectionCardExists) {
        try {
            $card = Get-Content -LiteralPath $cardPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $result['CardLastWriteUtc'] = (Get-Item -LiteralPath $cardPath).LastWriteTimeUtc.ToString('o')
            $result['CardAuthMode'] = (Select-SafeSettings @{ AUTH_MODE = [string]$card.authMode }).AUTH_MODE
            $result['CardLocalPort'] = if ($card.localUrl) { ([Uri]$card.localUrl).Port } else { $null }
            $result['CardAndFileKeyMatch'] = if ($values['CHAT_API_TOKEN'] -and $card.apiKey) { $values['CHAT_API_TOKEN'] -ceq $card.apiKey } else { $null }
        } catch { $result['CardReadError'] = $_.Exception.GetType().Name }
    }
    return $result
}

function Get-ErrorSignal([string]$Text) {
    $signals = @()
    foreach ($pattern in @('WinError\s+\d+', 'Errno\s+\d+', '\b0x[0-9A-Fa-f]+\b', '\b[A-Za-z_.]*(?:Error|Exception)\b', 'address already in use', 'Access is denied', 'permission denied', 'Failed to load Python DLL', 'not found', 'invalid client', 'Unauthorized', 'not signed in')) {
        foreach ($match in [regex]::Matches($Text, $pattern, 'IgnoreCase')) { $signals += $match.Value }
    }
    return ($signals | Select-Object -Unique) -join '; '
}

function Select-LogSignals([string[]]$Lines) {
    foreach ($line in $Lines) {
        if ($line -match '^\d{4}-\d{2}-\d{2}.*\b(?:INFO|WARNING|ERROR|DEBUG)\s+copilot_bridge(?:\.[A-Za-z_]+)?:\s*(?<message>.*)$') {
            $message = $Matches.message
            if ($message -match '^(\[startup\s|Shutting down|Dev Tunnel not running|Dev Tunnel re-established|Mode:|Reloaded Entra allow-list|AUTH_MODE|Unknown AUTH_MODE|Control Panel failed|server thread failed|GUI startup failed|setup wizard failed|provisioning failed)') {
                if ($message -match 'failed') {
                    $stamp = $line.Substring(0, [Math]::Min(23, $line.Length))
                    "$stamp failure: $(Get-ErrorSignal $message) (free-form detail omitted)"
                } else { Protect-Text $line }
            } elseif ($message -match '^Native session scan slow: duration_ms=\d+ sessions=\d+$') {
                Protect-Text $line
            }
        } elseif ($line -match '^(?<stamp>\d{4}-\d{2}-\d{2}[^ ]*\s+[^ ]+)\s+INFO aiohttp.access:.*"(?<method>GET|POST) (?<path>/health|/api/webconfig|/api/control/status|/api/control/shutdown|/api/control/tunnel|/api/(?:control/)?sync/status)(?:\?[^ ]*)? HTTP/[^" ]+" (?<status>\d{3})\b') {
            '{0} {1} {2} HTTP {3}' -f $Matches.stamp, $Matches.method, $Matches.path, $Matches.status
        } elseif ($line -match '^Traceback \(') {
            'Traceback (most recent call last):'
        } elseif ($line -match '^\s+File "(?<file>[^"]+)", line (?<number>\d+), in (?<function>[\w<>]+)') {
            '  File {0}, line {1}, in {2}' -f (Protect-Text $Matches.file), $Matches.number, $Matches.function
        } elseif ($line -match '^[A-Za-z_.]*(?:Error|Exception):' -or $line -match '^\[PYI-\d+') {
            'Exception: {0} (free-form detail omitted)' -f (Get-ErrorSignal $line)
        }
    }
}

function Get-BridgeProcesses {
    $filter = "Name='CopilotBridgeServer.exe' OR Name='devtunnel.exe' OR Name='python.exe' OR Name='pythonw.exe'"
    @(Get-CimInstance Win32_Process -Filter $filter | Where-Object {
        $_.Name -ieq 'CopilotBridgeServer.exe' -or ($_.CommandLine -match '(?i)CopilotBridge|copilot-bridge')
    })
}

function Select-ProcessInfo($Process) {
    $executable = $Process.ExecutablePath
    $role = if ($Process.CommandLine -match '--control-panel|--panel\b') { 'control-panel' }
            elseif ($Process.Name -ieq 'devtunnel.exe') { 'dev-tunnel' }
            elseif ($Process.CommandLine -match '--show-info|--connection-info') { 'connection-info' }
            else { 'server-or-source-process' }
    $version = if ($executable -and (Test-Path -LiteralPath $executable -PathType Leaf)) { (Get-Item -LiteralPath $executable).VersionInfo.ProductVersion } else { $null }
    [ordered]@{
        ProcessId = $Process.ProcessId; ParentProcessId = $Process.ParentProcessId
        CreatedUtc = if ($Process.CreationDate) { $Process.CreationDate.ToUniversalTime().ToString('o') } else { $null }
        Name = $Process.Name; Role = $role; Executable = Protect-Text $executable
        Version = Protect-Text $version
    }
}

if ($SelfTest) {
    $sensitive = 'API Key : private-test-key; Authorization: Bearer private-auth-value https://example.test/?token=secret person@example.com C:\Users\private-user\AppData'
    $clean = Protect-Text $sensitive
    foreach ($forbidden in @('private-test-key', 'private-auth-value', 'example.test', 'person@example.com', 'private-user')) {
        if ($clean.Contains($forbidden)) { throw "Redaction failed for $forbidden" }
    }
    $safe = Select-SafeSettings @{ AUTH_MODE = 'entra'; PORT = '13978'; CHAT_API_TOKEN = 'fixture-secret'; HOST = 'customer-private-host'; ENTRA_ALLOWED_USERS = 'private@example.com' } | ConvertTo-Json
    foreach ($forbidden in @('fixture-secret', 'customer-private-host', 'private@example.com')) {
        if ($safe.Contains($forbidden)) { throw 'Settings leaked a private value' }
    }
    $sample = @(
        '2026-09-12 10:00:00,000 INFO copilot_bridge.launcher: [startup 1/6] Loading configuration',
        '2026-09-12 10:00:01,000 INFO aiohttp.access: 127.0.0.1 "GET /api/control/status HTTP/1.1" 401 194 "-" "test"',
        '2026-09-12 10:00:02,000 INFO copilot_bridge.launcher: Shutting down...',
        'Traceback (most recent call last):',
        '  File "C:\Users\private-user\app\launcher.py", line 12, in main',
        'OSError: [WinError 10048] address already in use private-details',
        '2026-09-12 10:00:03,000 INFO copilot_bridge.launcher: API Key : never-export-this',
        '2026-09-12 10:00:04,000 INFO copilot_bridge.runner: Running prompt secret-chat-content',
        '2026-09-12 10:00:04,100 WARNING copilot_bridge.session_watcher: Native session scan slow: duration_ms=16340 sessions=52',
        '2026-09-12 10:00:04,200 WARNING copilot_bridge.session_watcher: Native session scan slow: duration_ms=16340 sessions=52 secret-chat-content',
        'private conversation line',
        '2026-09-12 10:00:05,000 INFO aiohttp.access: 127.0.0.1 "GET /api/sessions/private-id HTTP/1.1" 200 194 "-" "test"'
    )
    $signals = @(Select-LogSignals $sample) -join "`n"
    if ($signals -notmatch 'HTTP 401' -or $signals -notmatch 'WinError 10048' -or $signals -notmatch 'startup 1/6') { throw 'Required diagnostics were lost' }
    if ($signals -notmatch 'Native session scan slow: duration_ms=16340 sessions=52') { throw 'Slow scan timing was lost' }
    foreach ($forbidden in @('never-export-this', 'secret-chat-content', 'private conversation', 'private-id', 'private-details', 'private-user')) {
        if ($signals.Contains($forbidden)) { throw 'Log selector leaked private content' }
    }
    $fixture = Join-Path ([IO.Path]::GetTempPath()) ('cb-diagnostic-test-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($fixture) | Out-Null
    try {
        [IO.File]::WriteAllText((Join-Path $fixture '.env'), "AUTH_MODE='entra' # legacy`nPORT=13978`nCHAT_API_TOKEN=fixture-secret`n")
        [IO.File]::WriteAllText((Join-Path $fixture 'connection.json'), '{"authMode":"tunnel","localUrl":"http://localhost:3978","apiKey":"fixture-secret"}')
        $config = Get-FileConfig $fixture
        if (-not $config.CardAndFileKeyMatch -or $config.Settings.AUTH_MODE -ne 'entra' -or $config.CardLocalPort -ne 3978) { throw 'Configuration comparison failed' }
        if (($config | ConvertTo-Json -Depth 5).Contains('fixture-secret')) { throw 'Connection card leaked a key' }
    } finally { Remove-Item -LiteralPath $fixture -Recurse -Force }
    Write-Output 'DIAGNOSTIC_SELF_TEST_PASSED: redaction, log selection, settings, key comparison; no network or live processes'
    return
}

$report = [ordered]@{
    Schema = 1; CollectedUtc = [DateTime]::UtcNow.ToString('o'); RequestedPort = $Port
    Safety = 'Read-only inspection. No HTTP/network probes, server/tunnel control, database or chat content. No raw keys/configuration/command lines.'
    EnvironmentNote = 'Process scope is the collector shell, NOT the running server. Inherited server environment overrides .env; compare launch times, config timestamps and logs.'
    PowerShell = $PSVersionTable.PSVersion.ToString(); Errors = @()
}
$processes = @()
try { $processes = @(Get-BridgeProcesses) } catch { $report.Errors += "Process query: $($_.Exception.GetType().Name)" }
$report['ProcessesAtStart'] = @($processes | ForEach-Object { Select-ProcessInfo $_ })
$report['EnvironmentSources'] = @('Process', 'User', 'Machine') | ForEach-Object {
    $scope = $_
    $values = @{}
    foreach ($name in @('AUTH_MODE', 'HOST', 'PORT', 'TUNNEL_AUTH', 'TUNNEL_ENABLED', 'ONEDRIVE_SYNC_ENABLED', 'SESSION_WATCHER_ENABLED', 'CB_NO_GUI', 'CHAT_API_TOKEN', 'ENTRA_TENANT_ID', 'ENTRA_CLIENT_ID', 'ENTRA_AUDIENCE', 'ENTRA_ALLOWED_USERS', 'ENTRA_ALLOWED_GROUPS', 'ENTRA_ALLOWED_ROLES', 'ONEDRIVE_CLIENT_ID')) {
        $value = [Environment]::GetEnvironmentVariable($name, $scope)
        if ($null -ne $value) { $values[$name] = $value }
    }
    [ordered]@{ Scope = $scope; Settings = Select-SafeSettings $values }
}
$directories = @([IO.Path]::GetFullPath($DataDirectory))
$installDirectories = @()
foreach ($process in $processes) {
    if ($process.Name -ieq 'CopilotBridgeServer.exe' -and $process.ExecutablePath) {
        $directory = Split-Path $process.ExecutablePath -Parent
        $installDirectories += $directory
        if ((Test-Path -LiteralPath (Join-Path $directory '.env')) -or (Test-Path -LiteralPath (Join-Path $directory 'portable'))) { $directories += $directory }
    }
}
foreach ($parent in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($parent) {
        $directory = Join-Path $parent 'CopilotBridge'
        if (Test-Path -LiteralPath $directory) {
            $installDirectories += $directory
            if ((Test-Path -LiteralPath (Join-Path $directory '.env')) -or (Test-Path -LiteralPath (Join-Path $directory 'portable'))) { $directories += $directory }
        }
    }
}
$report['Installations'] = @($installDirectories | Sort-Object -Unique | ForEach-Object {
    $executable = Join-Path $_ 'CopilotBridgeServer.exe'
    [ordered]@{ Directory = Protect-Text $_; Version = if (Test-Path -LiteralPath $executable) { (Get-Item -LiteralPath $executable).VersionInfo.ProductVersion } else { $null }; PortableMarker = Test-Path -LiteralPath (Join-Path $_ 'portable') }
})
$report['Configurations'] = @()
$report['LogSignals'] = @()
foreach ($directory in $directories | Sort-Object -Unique) {
    try { $report.Configurations += Get-FileConfig $directory } catch { $report.Errors += "Config read: $($_.Exception.GetType().Name)" }
    foreach ($name in @('server.log', 'launcher.log')) {
        $path = Join-Path $directory "logs\$name"
        $log = [ordered]@{ Path = Protect-Text $path; Exists = Test-Path -LiteralPath $path -PathType Leaf }
        if ($log.Exists) {
            try {
                $item = Get-Item -LiteralPath $path
                $signals = @(Select-LogSignals @(Get-Content -LiteralPath $path -Tail 8000 -Encoding UTF8))
                $log['LastWriteUtc'] = $item.LastWriteTimeUtc.ToString('o')
                $log['Bytes'] = $item.Length
                $log['StartupEntriesInTail'] = @($signals | Where-Object { $_ -match '\[startup 1/' }).Count
                $log['Signals'] = @($signals | Select-Object -Last 500)
            } catch { $log['ReadError'] = $_.Exception.GetType().Name }
        }
        $report.LogSignals += $log
    }
}
try {
    $report['Listeners'] = @(Get-NetTCPConnection -State Listen | Where-Object {
        $_.LocalPort -eq $Port -or $_.OwningProcess -in @($processes.ProcessId)
    } | Select-Object LocalAddress, LocalPort, OwningProcess)
} catch { $report.Errors += "Listener query: $($_.Exception.GetType().Name)" }
try {
    $report['ParentProcesses'] = @($processes.ParentProcessId | Sort-Object -Unique | ForEach-Object {
        Get-CimInstance Win32_Process -Filter "ProcessId=$_" | ForEach-Object {
            [ordered]@{ ProcessId = $_.ProcessId; Name = $_.Name; Executable = Protect-Text $_.ExecutablePath }
        }
    })
} catch { $report.Errors += "Parent query: $($_.Exception.GetType().Name)" }
try {
    $report['WindowsCrashEvents'] = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1001; StartTime = (Get-Date).AddDays(-2) } -MaxEvents 300 -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -match '(?i)CopilotBridgeServer\.exe|devtunnel\.exe'
    } | Select-Object -First 15 | ForEach-Object {
        [ordered]@{ TimeUtc = $_.TimeCreated.ToUniversalTime().ToString('o'); Id = $_.Id; Provider = $_.ProviderName; Message = Protect-Text $_.Message }
    })
} catch { $report.Errors += "Windows events: $($_.Exception.GetType().Name)" }
try {
    $report['ScheduledTasks'] = @(Get-ScheduledTask | Where-Object {
        $_.TaskName -match 'Copilot.?Bridge' -or ($_.Actions | Where-Object { "$($_.Execute) $($_.Arguments)" -match 'Copilot.?Bridge|serve-background\.ps1' })
    } | ForEach-Object {
        [ordered]@{ Name = Protect-Text $_.TaskName; State = [string]$_.State; RestartCount = $_.Settings.RestartCount; RestartInterval = [string]$_.Settings.RestartInterval; Executables = @($_.Actions | ForEach-Object { Protect-Text $_.Execute }) }
    })
} catch { $report.Errors += "Scheduled tasks: $($_.Exception.GetType().Name)" }
try {
    $report['StartupEntries'] = @(Get-CimInstance Win32_StartupCommand | Where-Object { $_.Command -match 'Copilot.?Bridge|serve-background\.ps1' } | ForEach-Object {
        [ordered]@{ Name = Protect-Text $_.Name; Location = Protect-Text $_.Location; SourceLauncher = [bool]($_.Command -match 'serve-background\.ps1|launcher\.py'); FrozenLauncher = [bool]($_.Command -match 'CopilotBridgeServer\.exe') }
    })
} catch { $report.Errors += "Startup entries: $($_.Exception.GetType().Name)" }
try { $report['ProcessesAtEnd'] = @(Get-BridgeProcesses | ForEach-Object { Select-ProcessInfo $_ }) } catch { $report.Errors += "Final process query: $($_.Exception.GetType().Name)" }
$report['FinishedUtc'] = [DateTime]::UtcNow.ToString('o')
$report['NextStep'] = 'Run on the affected PC while the symptom is present. A second report helps compare PIDs/creation times. Do not send raw .env, connection.json, databases or chat transcripts.'
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$stem = 'CopilotBridge-diagnostics-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 6)
$jsonPath = Join-Path $OutputDirectory ($stem + '.json')
$zipPath = Join-Path $OutputDirectory ($stem + '.zip')
$json = $report | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($jsonPath, $json, [Text.UTF8Encoding]::new($false))
Compress-Archive -LiteralPath $jsonPath -DestinationPath $zipPath
Write-Output "Sanitized report: $jsonPath"
Write-Output "Share this ZIP: $zipPath"
Write-Output 'No server, tunnel, authentication setting, or database was changed. No network request was made.'
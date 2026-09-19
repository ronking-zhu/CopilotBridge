<# Runs only deterministic offline regressions; never live model/3978 probes. #>
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root 'server\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create server\.venv and install server\requirements.txt first.' }

# Keep the allowlist explicit: local_test calls a real model, and
# local_channel_test POSTs to the production server's default port.
$modules = @(
    'control_panel_sync', 'dashboard_api', 'filesystem_transport', 'first_use_auth',
    'history_api', 'identity_provider', 'image_upload', 'knowledge_api',
    'knowledge_extraction', 'native_session_discovery', 'notification_setup',
    'onedrive_auth', 'onedrive_local', 'onedrive_transport', 'session_store',
    'session_watcher', 'sync_engine', 'sync_service', 'timeline_api', 'tunnel_auth_mode'
)
foreach ($module in $modules) {
    $test = Join-Path $root "server\tests\${module}_test.py"
    $output = & $python $test 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Output $output
        throw "Failed: ${module}_test.py"
    }
    Write-Output "PASS ${module}_test.py"
}
Write-Output "OFFLINE_MODULES_PASSED=$($modules.Count) (live model/channel scripts excluded)"
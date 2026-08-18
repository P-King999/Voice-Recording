$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$AgentPidPath = Join-Path $RuntimeDir "agent.pid"

if (-not (Test-Path -LiteralPath $Pythonw)) {
    throw "Virtual environment not found. Follow the first-install steps in README.md."
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

function Test-AgentProcess {
    if (-not (Test-Path -LiteralPath $AgentPidPath)) { return $false }
    $ProcessId = (Get-Content -LiteralPath $AgentPidPath -Raw).Trim()
    if ($ProcessId -notmatch '^\d+$') { return $false }
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    return $null -ne $Process -and $Process.CommandLine -like "*agent.py*"
}

if (Test-AgentProcess) { exit 0 }

$LegacyProcesses = @{
    "recorder_watchdog" = "recorder_watchdog.py"
    "worker" = "worker.py"
    "summary_scheduler" = "summary_scheduler.py"
    "keep_awake" = "keep_awake.py"
    "power_manager" = "power_manager.py"
    "dashboard" = "streamlit"
    "reporter" = "reporter.py"
    "mail_query" = "mail_query.py"
    "recorder" = "recorder.py"
}
foreach ($Name in @("recorder_watchdog", "worker", "summary_scheduler", "keep_awake", "power_manager", "dashboard", "reporter", "mail_query", "recorder")) {
    $PidPath = Join-Path $RuntimeDir "${Name}.pid"
    if (-not (Test-Path -LiteralPath $PidPath)) { continue }
    $ProcessId = (Get-Content -LiteralPath $PidPath -Raw).Trim()
    $ManagedProcess = if ($ProcessId -match '^\d+$') {
        Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    }
    if ($null -ne $ManagedProcess -and $ManagedProcess.CommandLine -like "*$($LegacyProcesses[$Name])*") {
        if ($Name -eq "recorder") {
            New-Item -ItemType File -Path (Join-Path $RuntimeDir "recorder.stop") -Force | Out-Null
            $Deadline = (Get-Date).AddSeconds(20)
            while ((Get-Date) -lt $Deadline -and $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
                Start-Sleep -Milliseconds 500
            }
            if ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
                & "$env:WINDIR\System32\taskkill.exe" /PID $ProcessId /T /F | Out-Null
            }
        } else {
            & "$env:WINDIR\System32\taskkill.exe" /PID $ProcessId /T /F | Out-Null
        }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $AgentPidPath -Force -ErrorAction SilentlyContinue
$StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
$StartInfo.FileName = $Pythonw
$StartInfo.Arguments = "agent.py"
$StartInfo.WorkingDirectory = $ProjectRoot
$StartInfo.UseShellExecute = $false
$StartInfo.CreateNoWindow = $true
[System.Diagnostics.Process]::Start($StartInfo) | Out-Null

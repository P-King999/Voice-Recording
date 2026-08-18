$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"

$AgentPidPath = Join-Path $RuntimeDir "agent.pid"
$StopRequestPath = Join-Path $RuntimeDir "agent.stop"
if (-not (Test-Path -LiteralPath $AgentPidPath)) { exit 0 }
$ProcessId = (Get-Content -LiteralPath $AgentPidPath -Raw).Trim()
if ($ProcessId -notmatch '^\d+$') { exit 0 }
$Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
if ($null -eq $Process -or $Process.CommandLine -notlike "*agent.py*") {
    Remove-Item -LiteralPath $AgentPidPath -Force -ErrorAction SilentlyContinue
    exit 0
}

New-Item -ItemType File -Path $StopRequestPath -Force | Out-Null
$Deadline = (Get-Date).AddSeconds(35)
while ((Get-Date) -lt $Deadline) {
    if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $AgentPidPath -Force -ErrorAction SilentlyContinue
        exit 0
    }
    Start-Sleep -Seconds 1
}

& "$env:WINDIR\System32\taskkill.exe" /PID $ProcessId /T /F | Out-Null
Remove-Item -LiteralPath $AgentPidPath -Force -ErrorAction SilentlyContinue
throw "Voice Recorder Agent did not stop gracefully and was forcefully terminated."

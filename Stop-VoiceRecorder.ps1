$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"

${ExpectedArguments} = @{
    "recorder" = "recorder.py"
    "recorder_watchdog" = "recorder_watchdog.py"
    "worker" = "worker.py"
    "summary_scheduler" = "summary_scheduler.py"
    "keep_awake" = "keep_awake.py"
    "power_manager" = "power_manager.py"
    "dashboard" = "streamlit"
    "reporter" = "reporter.py"
    "mail_query" = "mail_query.py"
}

foreach ($Name in $ExpectedArguments.Keys) {
    $PidFile = Join-Path $RuntimeDir "${Name}.pid"
    if (-not (Test-Path -LiteralPath $PidFile)) { continue }
    $ProcessId = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($ProcessId -match '^\d+$') {
        $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        if ($null -ne $Process -and $Process.CommandLine -like "*$($ExpectedArguments[$Name])*" ) {
            # The virtual-environment launcher can leave the actual Python child alive.
            # End the verified managed process and its descendants together.
            & "$env:WINDIR\System32\taskkill.exe" /PID $ProcessId /T /F | Out-Null
        }
    }
    Remove-Item -LiteralPath $PidFile -Force
}

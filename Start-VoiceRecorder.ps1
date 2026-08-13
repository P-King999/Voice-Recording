$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Pythonw)) {
    throw "Virtual environment not found. Follow the first-install steps in README.md."
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

function Test-ManagedProcess {
    param([string]$Name, [string]$ExpectedArgument)

    $PidFile = Join-Path $RuntimeDir "${Name}.pid"
    if (-not (Test-Path -LiteralPath $PidFile)) { return $false }
    $ProcessId = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($ProcessId -notmatch '^\d+$') { return $false }
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    return $null -ne $Process -and $Process.CommandLine -like "*$ExpectedArgument*"
}

function Start-ManagedProcess {
    param([string]$Name, [string]$ExpectedArgument, [string[]]$Arguments)

    if (Test-ManagedProcess -Name $Name -ExpectedArgument $ExpectedArgument) { return }
    $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $Pythonw
    $StartInfo.Arguments = [string]::Join(" ", $Arguments)
    $StartInfo.WorkingDirectory = $ProjectRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $Process = [System.Diagnostics.Process]::Start($StartInfo)
    Set-Content -LiteralPath (Join-Path $RuntimeDir "${Name}.pid") -Value $Process.Id -NoNewline
}

Start-ManagedProcess -Name "recorder" -ExpectedArgument "recorder.py" -Arguments @("recorder.py")
Start-ManagedProcess -Name "recorder_watchdog" -ExpectedArgument "recorder_watchdog.py" -Arguments @("recorder_watchdog.py")
Start-ManagedProcess -Name "worker" -ExpectedArgument "worker.py" -Arguments @("worker.py")
Start-ManagedProcess -Name "summary_scheduler" -ExpectedArgument "summary_scheduler.py" -Arguments @("summary_scheduler.py")
Start-ManagedProcess -Name "keep_awake" -ExpectedArgument "keep_awake.py" -Arguments @("keep_awake.py")
Start-ManagedProcess -Name "power_manager" -ExpectedArgument "power_manager.py" -Arguments @("power_manager.py")
Start-ManagedProcess -Name "dashboard" -ExpectedArgument "streamlit" -Arguments @("-m", "streamlit", "run", "dashboard.py", "--server.headless=true", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false")
Start-ManagedProcess -Name "reporter" -ExpectedArgument "reporter.py" -Arguments @("reporter.py")
Start-ManagedProcess -Name "mail_query" -ExpectedArgument "mail_query.py" -Arguments @("mail_query.py")

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"

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
Start-ManagedProcess -Name "worker" -ExpectedArgument "worker.py" -Arguments @("worker.py")
Start-ManagedProcess -Name "keep_awake" -ExpectedArgument "keep_awake.py" -Arguments @("keep_awake.py")
Start-ManagedProcess -Name "dashboard" -ExpectedArgument "streamlit" -Arguments @("-m", "streamlit", "run", "dashboard.py", "--server.headless=true", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false")

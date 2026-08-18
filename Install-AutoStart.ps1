$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ProjectRoot "Launch-VoiceRecorder.vbs"
$Python = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $Launcher)) { throw "Background launcher not found." }
if (-not (Test-Path -LiteralPath $Python)) { throw "Virtual environment not found. Follow the first-install steps in README.md." }

$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$Command = "`"$env:WINDIR\System32\wscript.exe`" `"$Launcher`""
New-ItemProperty -Path $RunKey -Name "VoiceRecorder" -Value $Command -PropertyType String -Force | Out-Null

$TaskName = "Voice Recorder - Start at Sign-in"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$ProjectRoot\agent.py`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Trigger.Delay = "PT30S"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "Auto-start has been installed with a sign-in task and a registry fallback for the current user."

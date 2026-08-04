$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ProjectRoot "Launch-VoiceRecorder.vbs"
if (-not (Test-Path -LiteralPath $Launcher)) { throw "Background launcher not found." }

$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$Command = "`"$env:WINDIR\System32\wscript.exe`" `"$Launcher`""
New-ItemProperty -Path $RunKey -Name "VoiceRecorder" -Value $Command -PropertyType String -Force | Out-Null
Write-Host "Auto-start has been installed for the current user."

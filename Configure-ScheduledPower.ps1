param(
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$ShutdownTime = "21:00"
)

$ErrorActionPreference = "Stop"

$ShutdownTask = "Voice Recorder - Daily Shutdown"

$shutdownAction = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\shutdown.exe" `
    -Argument "/s /t 60 /d p:0:0 /c `"Voice Recorder scheduled shutdown`""
$shutdownTrigger = New-ScheduledTaskTrigger -Daily -At $ShutdownTime
$systemSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName $ShutdownTask -Action $shutdownAction -Trigger $shutdownTrigger `
    -Settings $systemSettings -User "SYSTEM" -RunLevel Highest -Force | Out-Null

Write-Host "Scheduled daily shutdown at $ShutdownTime."

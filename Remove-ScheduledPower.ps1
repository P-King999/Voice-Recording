$ErrorActionPreference = "Stop"
"Voice Recorder - Daily Shutdown", "Voice Recorder - Daily Wake" | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host "Scheduled power tasks removed."

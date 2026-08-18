$ErrorActionPreference = "Stop"

# Automatic shutdown is intentionally unsupported. Clean up a task created by
# older versions so running this script can never schedule a future shutdown.
Unregister-ScheduledTask -TaskName "Voice Recorder - Daily Shutdown" -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "Voice Recorder - Daily Wake" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Automatic shutdown is disabled; any legacy Voice Recorder power tasks were removed."

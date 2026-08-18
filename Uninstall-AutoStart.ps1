$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $RunKey -Name "VoiceRecorder" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "Voice Recorder - Start at Sign-in" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Auto-start task and registry fallback have been removed for the current user."

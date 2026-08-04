$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $RunKey -Name "VoiceRecorder" -ErrorAction SilentlyContinue
Write-Host "Auto-start has been removed for the current user."

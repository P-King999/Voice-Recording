$ErrorActionPreference = "Stop"

# Change only the plugged-in idle sleep timeout; battery policy stays unchanged.
& "$env:WINDIR\System32\powercfg.exe" /change standby-timeout-ac 0
if ($LASTEXITCODE -ne 0) { throw "Could not update the Windows power policy." }
Write-Host "Plugged-in idle sleep is disabled. Configure lid-close behavior separately in Windows Power Options."

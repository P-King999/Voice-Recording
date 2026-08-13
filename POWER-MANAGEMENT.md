# Power management

The recorder now starts `power_manager.py` with the other background services.
When Windows reports that the laptop is running on battery, it switches to the
Windows power saver scheme. When AC power is restored, it restores the scheme
that was active before the switch.

Run PowerShell as Administrator once to install the daily shutdown schedule. If
PowerShell blocks local scripts, use `-ExecutionPolicy Bypass` for this one run;
this does not permanently change the machine policy:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Configure-ScheduledPower.ps1
```

The default schedule is shutdown at `21:00`. The time can be changed, for example:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Configure-ScheduledPower.ps1 -ShutdownTime 21:00
```

The shutdown uses a 60-second countdown so the last recording segment can be
closed. Remove the task with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Remove-ScheduledPower.ps1
```

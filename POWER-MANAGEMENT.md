# Power management

The recorder starts `power_manager.py` with the other background services.
When Windows reports that the laptop is running on battery, it switches to the
Windows power saver scheme. When AC power is restored, it restores the scheme
that was active before the switch.

The project does not schedule shutdown, restart, or account sign-out. Running
`Configure-ScheduledPower.ps1` removes any legacy Voice Recorder power tasks.

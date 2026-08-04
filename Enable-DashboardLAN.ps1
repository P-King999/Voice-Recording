$ErrorActionPreference = "Stop"

$RuleName = "Voice Recorder Dashboard (LAN)"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell window so Windows Firewall can allow dashboard access on your local network."
}

$policy = New-Object -ComObject HNetCfg.FwPolicy2
try {
    $policy.Rules.Remove($RuleName)
} catch {
    # The rule does not exist yet.
}

$rule = New-Object -ComObject HNetCfg.FWRule
$rule.Name = $RuleName
$rule.Description = "Allows private-network access to the Voice Recorder dashboard."
$rule.Protocol = 6 # TCP
$rule.LocalPorts = "8501"
$rule.Direction = 1 # Inbound
$rule.Action = 1 # Allow
$rule.Profiles = 2 # Private
$rule.Enabled = $true
$policy.Rules.Add($rule)

Write-Host "Private-network firewall access enabled for the dashboard on TCP port 8501."

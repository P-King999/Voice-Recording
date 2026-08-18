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

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvironmentPath = Join-Path $ProjectRoot ".env"
$lines = if (Test-Path -LiteralPath $EnvironmentPath) { Get-Content -LiteralPath $EnvironmentPath } else { @() }
$updated = $false
$lines = foreach ($line in $lines) {
    if ($line -match '^\s*DASHBOARD_BIND_ADDRESS=') {
        $updated = $true
        "DASHBOARD_BIND_ADDRESS=0.0.0.0"
    } else {
        $line
    }
}
if (-not $updated) { $lines += "DASHBOARD_BIND_ADDRESS=0.0.0.0" }
Set-Content -LiteralPath $EnvironmentPath -Value $lines -Encoding utf8

Write-Host "Private-network firewall access enabled. Restart Voice Recorder to apply DASHBOARD_BIND_ADDRESS=0.0.0.0."

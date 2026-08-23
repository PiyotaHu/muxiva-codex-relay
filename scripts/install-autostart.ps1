$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repo "scripts\start-relay.ps1"
$s1Launcher = Join-Path $repo "scripts\start-s1-mini.ps1"
$tunnelLauncher = Join-Path $repo "scripts\start-pi-tunnel.ps1"
$enablePiBridge = $env:MUXIVA_ENABLE_PI_BRIDGE -in @("1", "true", "yes", "on")
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$s1Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$s1Launcher`""
$tunnelAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$tunnelLauncher`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
try {
    Register-ScheduledTask -TaskName "MuxivaCodexRelay" -Action $action -Trigger $trigger -Settings $settings -Description "LAN relay for ESP32 Codex tasks" -Force -ErrorAction Stop | Out-Null
    Register-ScheduledTask -TaskName "MuxivaS1Mini" -Action $s1Action -Trigger $trigger -Settings $settings -Description "S1-mini by Superwhisper local normalizer" -Force -ErrorAction Stop | Out-Null
    if ($enablePiBridge -and (Test-Path (Join-Path $repo "runtime\pi_bridge_ed25519_nopass"))) {
        Register-ScheduledTask -TaskName "MuxivaPiTunnel" -Action $tunnelAction -Trigger $trigger -Settings $settings -Description "Reverse tunnel for ESP32 Codex relay" -Force -ErrorAction Stop | Out-Null
    }
    Write-Host "Installed Muxiva Codex startup tasks."
} catch {
    # Standard users may not be allowed to register scheduled tasks. HKCU Run
    # provides a per-user, no-admin fallback with the same hidden launchers.
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $relayCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
    $s1Command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$s1Launcher`""
    $tunnelCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$tunnelLauncher`""
    New-ItemProperty -Path $runKey -Name "MuxivaCodexRelay" -Value $relayCommand -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $runKey -Name "MuxivaS1Mini" -Value $s1Command -PropertyType String -Force | Out-Null
    if ($enablePiBridge -and (Test-Path (Join-Path $repo "runtime\pi_bridge_ed25519_nopass"))) {
        New-ItemProperty -Path $runKey -Name "MuxivaPiTunnel" -Value $tunnelCommand -PropertyType String -Force | Out-Null
    }
    Write-Host "Scheduled tasks require admin; installed per-user startup entries instead."
}

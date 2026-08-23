$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$key = Join-Path $repo "runtime\pi_bridge_ed25519_nopass"
$envFile = Join-Path $repo ".env"
if (-not $env:MUXIVA_PI_SSH_TARGET -and (Test-Path $envFile)) {
    $targetLine = Get-Content $envFile | Where-Object { $_ -match '^MUXIVA_PI_SSH_TARGET=' } | Select-Object -First 1
    if ($targetLine) {
        $env:MUXIVA_PI_SSH_TARGET = ($targetLine -split '=', 2)[1].Trim()
    }
}
$target = if ($env:MUXIVA_PI_SSH_TARGET) { $env:MUXIVA_PI_SSH_TARGET } else { "pi@raspberrypi.local" }

while ($true) {
    if (-not (Test-Path $key)) {
        Write-Error "Pi bridge key is missing: $key"
        Start-Sleep -Seconds 30
        continue
    }
    & ssh.exe -i $key -o BatchMode=yes -o ExitOnForwardFailure=yes `
        -o ServerAliveInterval=20 -o ServerAliveCountMax=3 `
        -N -R 18765:127.0.0.1:8765 $target
    Start-Sleep -Seconds 3
}

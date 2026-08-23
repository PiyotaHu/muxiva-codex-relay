param([switch]$Once)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $repo "src"
Set-Location $repo
do {
    # Keep redirected service logs live so BLE and app-server failures are
    # observable while the relay runs in a hidden background window.
    python -u -m muxiva_codex_relay
    $exitCode = $LASTEXITCODE
    if ($Once) { exit $exitCode }
    Write-Warning "muxiva-codex-relay exited with code $exitCode; restarting in 5 seconds"
    Start-Sleep -Seconds 5
} while ($true)

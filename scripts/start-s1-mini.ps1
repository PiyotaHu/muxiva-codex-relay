param([switch]$Once)

$ErrorActionPreference = "Stop"

$llama = Get-Command llama-server -ErrorAction SilentlyContinue
if (-not $llama) {
    $llama = Get-Command llama-server.exe -ErrorAction SilentlyContinue
}
if (-not $llama) {
    $repo = Split-Path -Parent $PSScriptRoot
    $bundled = Get-ChildItem -LiteralPath (Join-Path $repo "runtime\llama.cpp") -Filter "llama-server.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bundled) {
        $llama = $bundled
    } else {
        throw "llama-server is not installed. Run .\scripts\install-llama-cpp.ps1 first."
    }
}

$arguments = @(
    "-hf", "superwhisper/s1-mini-GGUF:Q4_K_M",
    "--host", "127.0.0.1",
    "--port", "8091",
    "--jinja",
    "--temp", "0"
)

# Start-Process strips nested JSON quotes from ArgumentList on Windows. The
# llama.cpp environment variable carries the exact same option losslessly.
$env:LLAMA_ARG_CHAT_TEMPLATE_KWARGS = '{"enable_thinking":false}'

$executable = if ($llama.Source) { $llama.Source } else { $llama.FullName }
$repo = Split-Path -Parent $PSScriptRoot
$stdoutLog = Join-Path $repo "runtime\s1-mini.stdout.log"
$stderrLog = Join-Path $repo "runtime\s1-mini.stderr.log"
do {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8091/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            Write-Host "S1-mini by Superwhisper is already healthy at http://127.0.0.1:8091/v1"
            exit 0
        }
    } catch {}

    $process = Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
    Write-Host "S1-mini by Superwhisper started at http://127.0.0.1:8091/v1 (PID $($process.Id))"
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    if ($Once) { exit $exitCode }
    Write-Warning "S1-mini exited with code $exitCode; restarting in 5 seconds"
    Start-Sleep -Seconds 5
} while ($true)

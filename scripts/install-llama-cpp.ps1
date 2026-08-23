$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$runtime = [System.IO.Path]::GetFullPath((Join-Path $repo "runtime\llama.cpp"))
$expectedRoot = [System.IO.Path]::GetFullPath((Join-Path $repo "runtime"))
if (-not $runtime.StartsWith($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to install outside the repository runtime directory"
}
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

$releases = Invoke-RestMethod -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10" -Headers @{"User-Agent"="muxiva-codex-relay"}
$release = $releases | Where-Object {
    $_.assets.name -match '^llama-b\d+-bin-win-cpu-x64\.zip$'
} | Select-Object -First 1
$asset = $release.assets | Where-Object { $_.name -match '^llama-b\d+-bin-win-cpu-x64\.zip$' } | Select-Object -First 1
if (-not $asset) {
    throw "The latest llama.cpp release has no Windows x64 CPU archive"
}

$archive = Join-Path $runtime $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -Headers @{"User-Agent"="muxiva-codex-relay"}
Expand-Archive -LiteralPath $archive -DestinationPath $runtime -Force
Remove-Item -LiteralPath $archive -Force

$server = Get-ChildItem -LiteralPath $runtime -Filter "llama-server.exe" -Recurse | Select-Object -First 1
if (-not $server) {
    throw "llama-server.exe was not found after extraction"
}
Write-Host "Installed llama.cpp $($release.tag_name): $($server.FullName)"

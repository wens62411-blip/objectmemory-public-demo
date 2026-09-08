param([switch]$SkipFirmware)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath (Join-Path $root 'apps\web')
try {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
if (-not $SkipFirmware) {
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'firmware-build.py')
    if ($LASTEXITCODE -ne 0) { throw 'Firmware build failed. See the saved build log.' }
}
Write-Host 'Build completed successfully.' -ForegroundColor Green

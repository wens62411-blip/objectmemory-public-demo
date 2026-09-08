param(
    [ValidatePattern('^https?://(127\.0\.0\.1|localhost):\d+$')][string]$Url = 'http://127.0.0.1:8033',
    [ValidateRange(0, 16)][int]$DeviceIndex = 0,
    [ValidateSet('DSHOW', 'MSMF', 'ANY')][string]$Backend = 'MSMF'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:OM_ACCEPTANCE_URL = $Url
$env:OM_ACCEPTANCE_SECONDS = '60'
$env:OM_CAMERA_INDEX = [string]$DeviceIndex
$env:OM_CAMERA_BACKEND = $Backend
Push-Location -LiteralPath (Join-Path $root 'apps\web')
try {
    & node.exe '.\scripts\real-camera-frontend-acceptance.mjs'
    if ($LASTEXITCODE -ne 0) { throw 'Real camera frontend acceptance failed. See data/diagnostics/real-camera-acceptance.json.' }
} finally {
    Pop-Location
}

param([switch]$SkipFirmware, [switch]$E2E, [switch]$VirtualDeviceE2E)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$testDataRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("objectmemory-test-" + [guid]::NewGuid().ToString('N'))
$env:OM_RUNTIME_MODE = 'TEST'
$env:OM_DATA_DIR = $testDataRoot
Push-Location -LiteralPath $root
try {
    New-Item -ItemType Directory -Force -Path (Join-Path $root 'data\verification') | Out-Null
    & (Join-Path $root '.venv\Scripts\python.exe') -m pytest apps/api/tests services/vision/tests -q --junitxml=data/verification/python-tests.xml
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    Push-Location -LiteralPath (Join-Path $root 'apps\web')
    try {
        & npm.cmd test -- --run
        if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed.' }
        if ($E2E) {
            & npx.cmd playwright install chromium
            if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed. Core application and non-browser tests remain available.' }
            & npm.cmd run test:e2e
            if ($LASTEXITCODE -ne 0) { throw 'Browser tests failed.' }
        }
    } finally { Pop-Location }
    if (-not $SkipFirmware) {
        & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'firmware-build.py')
        if ($LASTEXITCODE -ne 0) { throw 'Firmware compilation failed.' }
    }
    if ($VirtualDeviceE2E) {
        Write-Host 'Running the real virtual-device lifecycle (includes the 45-second offline timeout)...' -ForegroundColor Cyan
        & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'verify-virtual-device.py')
        if ($LASTEXITCODE -ne 0) { throw 'Virtual-device lifecycle acceptance failed.' }
    }
    Write-Host 'All selected tests passed.' -ForegroundColor Green
} finally {
    Pop-Location
    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $resolvedTest = [System.IO.Path]::GetFullPath($testDataRoot)
    if ($resolvedTest.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedTest)) {
        Remove-Item -LiteralPath $resolvedTest -Recurse -Force
    }
}

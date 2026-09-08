param([switch]$SkipFrontend, [switch]$SkipOptionalVision)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $candidates = @()
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $candidates += (& py -0p 2>&1 | ForEach-Object { if ($_ -match '(\S:\\.*python.exe)\s*$') { $matches[1] } })
        }
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += (Get-Command python).Source }
        $selectedPython = $null
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate) {
                $supported = & $candidate -c 'import sys; print(1 if (3,10) <= sys.version_info[:2] <= (3,13) else 0)'
                if ($supported -eq '1') {
                    $selectedPython = $candidate
                    $version = & $candidate -c 'import sys; print(sys.version_info.minor)'
                    if ($version -eq '12') { break }
                }
            }
        }
        if (-not $selectedPython) { throw 'Please install Python 3.12 from python.org, then run this script again.' }
        & $selectedPython -m venv (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
    }
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies failed to install. Read the error above and retry.' }
    $firmwarePython = Join-Path $root '.venv-firmware\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $firmwarePython)) {
        & $venvPython -m venv (Join-Path $root '.venv-firmware')
        if ($LASTEXITCODE -ne 0) { throw 'Firmware environment creation failed.' }
    }
    & $firmwarePython -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-firmware.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'PlatformIO installation failed.' }
    if (-not $SkipOptionalVision) {
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-vision.lock.txt')
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Optional hand model dependencies are unavailable. Stable tag mode remains available.' }
        & $venvPython (Join-Path $PSScriptRoot 'download-models.py') --all
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Optional model download failed. Stable tag mode remains available.' }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root '.env'))) {
        Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination (Join-Path $root '.env')
    }
    & $venvPython (Join-Path $PSScriptRoot 'generate-demo-assets.py')
    if ($LASTEXITCODE -ne 0) { throw 'Demo asset generation failed.' }
    & $venvPython (Join-Path $PSScriptRoot 'init-project.py')
    if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
    if (-not $SkipFrontend) {
        if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Please install Node.js 22 or newer from nodejs.org.' }
        Push-Location -LiteralPath (Join-Path $root 'apps\web')
        try {
            & npm.cmd ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
        } finally { Pop-Location }
    }
    Write-Host 'ObjectMemory is ready. Run scripts\start.ps1 or double-click Start.bat.' -ForegroundColor Green
} finally { Pop-Location }

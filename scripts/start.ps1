param([int]$Port = 8018, [switch]$NoBrowser, [switch]$Lan)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { & (Join-Path $PSScriptRoot 'bootstrap.ps1') }
if (-not (Test-Path -LiteralPath (Join-Path $root 'apps\web\dist\index.html'))) {
    Push-Location -LiteralPath (Join-Path $root 'apps\web')
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Build failed. Run bootstrap.ps1 first.' }
    } finally { Pop-Location }
}
$env:OM_RUNTIME_MODE = 'REAL'
$env:OM_RUN_MODE = 'REAL'
$arguments = @((Join-Path $PSScriptRoot 'serve.py'), '--port', $Port, '--mode', 'REAL')
if ($NoBrowser) { $arguments += '--no-browser' }
if ($Lan) { $arguments += '--lan' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'ObjectMemory stopped with an error. See the output above.' }

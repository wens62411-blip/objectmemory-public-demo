param([int]$Port = 8018, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { & (Join-Path $PSScriptRoot 'bootstrap.ps1') }
$env:OM_RUNTIME_MODE = 'REAL'
$arguments = @((Join-Path $PSScriptRoot 'serve.py'), '--dev', '--port', $Port, '--mode', 'REAL')
if ($NoBrowser) { $arguments += '--no-browser' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Development server stopped with an error.' }

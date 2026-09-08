[CmdletBinding()]
param(
    [ValidatePattern('^[0-9,-]+$')]
    [string]$Indices = '',
    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',
    [ValidateRange(30, 120)]
    [int]$Frames = 30,
    [ValidateRange(3, 30)]
    [int]$TimeoutSeconds = 8,
    [ValidateRange(5, 120)]
    [int]$OverallTimeoutSeconds = 45
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw '项目Python环境不存在。请先双击 Bootstrap.bat，或运行 scripts\bootstrap.ps1。'
}

$Script = Join-Path $PSScriptRoot 'camera_diagnose.py'
if (-not $Indices) {
    $Indices = if ($Profile -eq 'full') { '0-10' } else { '0' }
}
& $Python $Script '--indices' $Indices '--profile' $Profile '--frames' $Frames '--timeout' $TimeoutSeconds '--overall-timeout' $OverallTimeoutSeconds
exit $LASTEXITCODE

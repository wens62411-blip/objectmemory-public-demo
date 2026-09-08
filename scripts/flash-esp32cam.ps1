[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?i:COM[0-9]{1,3})$')]
    [string]$Port
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw '尚未找到项目 Python 环境。请先运行 scripts\bootstrap.ps1。'
}

& $Python (Join-Path $PSScriptRoot 'firmware-build.py') --flash --port $Port
if ($LASTEXITCODE -ne 0) {
    throw "ESP32-CAM 刷写失败（退出码 $LASTEXITCODE）。请查看 firmware\esp32cam\build\build.log。"
}

Write-Host '固件已由 PlatformIO 真实上传。接下来请在设备中心执行串口配网。' -ForegroundColor Green


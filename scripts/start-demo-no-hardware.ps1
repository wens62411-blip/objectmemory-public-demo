param(
    [ValidateRange(1024, 65535)][int]$Port = 8018,
    [ValidateSet('video', 'webcam')][string]$Source = 'video',
    [ValidateRange(0, 16)][int]$CameraIndex = 0,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & (Join-Path $PSScriptRoot 'bootstrap.ps1')
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'apps\web\dist\index.html'))) {
    Push-Location -LiteralPath (Join-Path $root 'apps\web')
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败，请查看上方日志。' }
    } finally {
        Pop-Location
    }
}

$arguments = @(
    (Join-Path $PSScriptRoot 'start_no_hardware_demo.py'),
    '--port', $Port,
    '--source', $Source,
    '--camera-index', $CameraIndex
)
if ($NoBrowser) { $arguments += '--no-browser' }
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw '无硬件演示没有成功启动或异常停止，请查看上方错误。'
}


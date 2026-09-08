param(
    [string]$BackendUrl = 'http://127.0.0.1:8018',
    [ValidateSet('video', 'webcam')][string]$Source = 'video',
    [ValidateRange(0, 16)][int]$CameraIndex = 0,
    [string]$DeviceName = '虚拟 ESP32-CAM',
    [string]$RoomName = '客厅'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '缺少项目 Python 环境，请先运行 scripts\bootstrap.ps1。'
}

$arguments = @(
    '-m', 'tools.virtual_esp32cam',
    '--backend-url', $BackendUrl,
    '--auto-enroll',
    '--source', $Source,
    '--camera-index', $CameraIndex,
    '--port', 8766,
    '--device-name', $DeviceName,
    '--room-name', $RoomName
)
if ($Source -eq 'video') {
    $videoPath = Join-Path $root 'demo\sample-videos\object-memory-demo.mp4'
    if (-not (Test-Path -LiteralPath $videoPath)) {
        $videoPath = Join-Path $root 'demo\sample-videos\object-memory-demo.avi'
    }
    $arguments += @('--video-path', $videoPath)
}

Push-Location -LiteralPath $root
try {
    Write-Host '正在启动 Virtual ESP32-CAM（虚拟设备，不代表真实硬件）...' -ForegroundColor Cyan
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw '虚拟设备停止并返回错误，请查看上方明确错误。'
    }
} finally {
    Pop-Location
}

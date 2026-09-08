# Virtual ESP32-CAM

这个工具是明确标注的**虚拟设备**，用于没有开发板时验证完整设备协议。它不创建假 COM 口，不运行 PlatformIO 上传，也不会把模拟结果写成真机刷写成功。

它与真实固件共用以下后端路径：

- 一次性 `POST /api/device-enrollment/claim`；
- 只返回一次的设备令牌；本地绑定文件相当于虚拟 NVS；
- 每 15 秒 `POST /api/devices/{device_id}/heartbeat`；
- 后端 45 秒心跳超时离线与重新上线逻辑；
- `/health`、`/device`、`/capture` 和 MJPEG `/stream`；
- 后台自动创建摄像头并接入现有视觉引擎。

默认解码 `demo/sample-videos/object-memory-demo.mp4`，文件不存在时回退 AVI。一个后台帧泵只解码一次，所有 HTTP 客户端共享最新 JPEG，避免视频探测和视觉引擎分别加速回放。每帧顶部写有 `VIRTUAL ESP32-CAM - NOT PHYSICAL HARDWARE`。

## 启动

先启动物忆，再运行：

```powershell
& ".\scripts\start-virtual-device.ps1" -BackendUrl "http://127.0.0.1:8018"
```

脚本只在后端本机自动创建一次性配对码，完整配对码和设备令牌不会输出到终端。令牌保存在 Git 已忽略的 `data/temporary/virtual-devices`。停止后，设备中心会在真实 45 秒超时到达时变为离线；再次运行会先使用保存的令牌恢复心跳。

使用电脑摄像头作为虚拟节点的视频来源：

```powershell
& ".\scripts\start-virtual-device.ps1" -Source webcam -CameraIndex 0
```

这种模式会真实占用摄像头。应先停止 ObjectMemory 的原生 OpenCV 摄像头，避免两个进程争用同一设备。

## 无硬件完整演示

双击 `Start-No-Hardware-Demo.bat`。该入口启动后端和静态前端，创建演示物品，启动虚拟设备，等待实际 MJPEG 画面驱动 ArUco 状态机，并把本次 placed 与搜索核对结果写到 `data/verification/no-hardware-demo.json`。

## 独立验收

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\verify-virtual-device.py"
```

这个命令使用独立数据库启动真实 FastAPI，验证配对、哈希令牌、两次 15 秒心跳、四个设备 HTTP 接口、视觉帧、事件截图和片段，然后停止虚拟设备并真实等待 45 秒离线，最后以同一设备和摄像头重新上线。结果保存到 `data/diagnostics/virtual-device-acceptance.json`。

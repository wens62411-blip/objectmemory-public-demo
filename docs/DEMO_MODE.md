# DEMO 模式

DEMO 用来在没有硬件时展示真实代码路径，但事件像素来自合成视频或虚拟设备。它不是 REAL 的替代证据。

页面在 DEMO 运行期间持续显示：

> 演示模式，当前事件不来自真实家庭摄像头。

## 启动

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-demo-no-hardware.ps1" -Port 8018
```

也可以双击 `Start-No-Hardware-Demo.bat`。入口会强制 `OM_RUNTIME_MODE=DEMO`，使用：

- 数据库 `data/database/objectmemory-demo.sqlite`；
- 媒体 `data/demo-media`；
- 演示物品、区域和测试视频摄像头；
- 明确标记为虚拟设备的 ESP32 协议模拟器。

切换自 REAL 时必须先停止原进程。DEMO 不能通过 URL 参数或前端状态写入 REAL 数据库。

## 演示数据来源

### 合成测试视频

`scripts/generate-demo-assets.py` 生成可重复的 ArUco 场景视频，画面本身带测试标识。视频文件来源统一记录为：

```text
runtime_mode = DEMO
source_type  = video_file
is_simulated = true
```

一次完整稳定—移动—稳定过程仍经过真实检测、跟踪、区域、状态机、媒体和 SQLite 代码；所以它可以验证软件链路，但不能证明物理摄像头或家庭物品识别。

循环回放每次重新开始必须建立新的 `source_session_id`。同一轮中的同一 movement 通过 movement session、帧范围、关键帧哈希和唯一约束去重，不能无限复制。

### 虚拟 ESP32

虚拟设备实现设备领取、token、心跳、JPEG/MJPEG 和命令协议，用于验证后端协议。它只允许在 DEMO/TEST 启动，并显示“虚拟设备 · 非真实硬件”：

```text
source_type  = virtual_esp32
is_simulated = true
```

即便虚拟进程以上游电脑摄像头作为像素源，它的设备身份和事件仍是模拟，不得出现在 REAL 搜索。

### demo seed

`POST /api/system/demo-seed` 只在 DEMO 可用。当前 seed 创建演示物品、摄像头和区域配置；它本身不应直接创建 movement。返回值标记 `source_type=demo_seed`、`is_simulated=true`。实际事件必须由视频链路产生，并标为 `video_file`。

## 可选上游

默认回放合成 MP4。要让虚拟设备读取电脑摄像头，可运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-demo-no-hardware.ps1" -Source webcam -CameraIndex 0
```

这仍是 DEMO，不是物理 ESP32，也不是 REAL 事件。该路径只适合协议和页面演示。

## 清空 DEMO

管理员存储页的“清空演示数据”与普通 retention、TEST 清理和危险的 REAL 删除分开。执行前可用“立即扫描”和“预览将删除内容”查看范围。注册参考图属于共享用户资产，普通 DEMO 清理不会当成事件媒体删除。

若要获得完全独立的新演示轮次，应停止进程、清空 DEMO 数据、重新启动，让新来源会话从空库建立。不要复制 DEMO 数据库到 REAL。

## 验收 DEMO 时应检查

1. `/api/runtime-config` 返回 `DEMO/true`。
2. seed 返回 `demo_seed/true`，没有写 REAL DB。
3. 视频 movement 返回 `video_file/true`。
4. 只产生期望数量的完整 `movement`，而不是 picked_up/moved/placed 多行。
5. 截图可解码、短片可读取帧，SHA-256 与文件一致。
6. 事件、截图和短片的 `source_session_id` 一致。
7. 页面始终显示 DEMO 横幅、测试视频或虚拟设备标签。
8. REAL 进程搜索不到这些记录。

测试是否实际执行及本轮数量见 [审计报告](AUDIT_REPORT.md)。单元 Mock 或静态页面截图不算上述验收。

本轮实际 DEMO 浏览器 E2E 已启动真实 FastAPI、独立 `objectmemory-demo.sqlite` 和真实合成视频文件，恰好生成 1 条 `DEMO/video_file/true` movement、1 张 116,595 B JPEG 和 1 段 33,275 B 短片；两份 SHA-256 与 API/SQLite 一致，媒体可解码，回绕后事件总数仍为 1。测试目录随后自动清理。这个结果证明 DEMO 软件闭环，不证明物理家庭摄像头。

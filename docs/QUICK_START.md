# 快速开始

本文只给出当前代码实际存在的入口。结果是否通过以命令的本次退出码为准。

## 1. 环境准备

推荐 Windows 10/11、Python 3.12、Node.js 22 或更新版本。首次安装需要联网；核心本地运行不需要云端 API。先在当前 ObjectMemory 项目根目录打开 PowerShell；文档不写死某个用户的绝对路径。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\bootstrap.ps1"
```

`bootstrap.ps1` 会创建 `.venv` 和独立的 `.venv-firmware`、安装锁定依赖、生成可重复的 ArUco 标签与合成演示视频、初始化空的 REAL 数据库并构建前端。生成演示素材不等于将演示事件写入 REAL 数据库。

## 2. 启动 REAL

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start.ps1" -Port 8018
```

打开 <http://127.0.0.1:8018>。`Start.bat` 等价于使用默认端口运行该脚本。

REAL 启动后应当满足：

1. `GET /api/runtime-config` 的 `runtime_mode` 为 `REAL`、`is_simulated` 为 `false`。
2. 数据库是 `data/database/objectmemory.sqlite`。
3. 媒体目录是 `data/real-media`。
4. 没有摄像头和已有真实证据时，事件数为 0，搜索显示“暂时没有真实摄像头产生的位置记录”。
5. `POST /api/system/demo-seed`、测试视频上传和虚拟设备启动在 REAL 中被拒绝。

停止服务请在启动窗口按 `Ctrl+C`，让采集线程释放摄像头并执行轻量清理。

## 3. 配置一条真实标签链路

1. 在“物品管理”新建物品，为它分配未重复的 ArUco ID（字典 `DICT_4X4_50`，范围 0–49）。
2. 打印 `demo/markers/aruco-markers-a4.svg` 或对应 PNG，按 100% 比例打印，保留完整白边。
3. 在“摄像头”添加电脑摄像头，索引通常从 `0` 开始。
4. 先点击“测试连接”。只有接口实际读取并返回可解码画面，才代表取帧成功；这仍不代表已识别物品。
5. 保存摄像头，为桌面、沙发等区域绘制互不混淆的归一化多边形。
6. 启动摄像头，让标签在旧位置稳定；跨区移动后在新位置继续稳定。
7. 在“事件时间线”确认只出现一条 movement 事件，并核对来源、截图、短片、帧范围、会话 ID 和哈希。

当前状态与历史事件的判定标准见 [真实事件验收](REAL_EVENT_ACCEPTANCE.md)。

## 4. 摄像头诊断

先停止所有正在使用同一物理摄像头的 ObjectMemory 来源，再运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\camera-diagnose.ps1"
```

日常优先从“高级工具 → 摄像头诊断”做快速检查：摄像头已运行时只读取这一路的实际状态，不抢占设备；未运行时后台检查默认索引 0，页面显示进度并可取消。需要全面排查才选择完整诊断，先停止同一摄像头的其他来源。诊断截图只证明该次取帧，不证明物品识别或移动事件。

## 5. 启动 DEMO

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-demo-no-hardware.ps1" -Port 8018
```

或双击 `Start-No-Hardware-Demo.bat`。该入口：

- 以 `DEMO` 重启后端；
- 使用 `data/database/objectmemory-demo.sqlite` 和 `data/demo-media`；
- 创建演示物品/区域/视频摄像头；
- 启动明确标记为“虚拟设备 · 非真实硬件”的设备进程；
- 默认让虚拟设备回放合成 MP4。

测试电脑摄像头作为虚拟设备上游时可以使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-demo-no-hardware.ps1" -Source webcam -CameraIndex 0
```

即便上游像素来自物理电脑摄像头，该进程仍是 DEMO，设备身份仍是虚拟设备，事件仍为 `is_simulated=true`，不能进入 REAL 证据。

## 6. 存储管理

打开 `/storage`：

- “立即扫描”只统计；
- “预览将删除内容”是 dry-run；
- “立即清理”执行当前策略；
- 可以切换 `MINIMAL`、`BALANCED`、`FORENSIC`；
- 可以固定/取消固定事件、导出固定事件；
- “清空演示数据”和“清空测试数据”与危险区“删除所有真实记录”严格分开。

删除 REAL 记录需要在本机 REAL 进程输入精确确认文本 `DELETE REAL DATA`。普通 retention 不删除注册参考图。

## 7. 测试与构建

快速软件门禁：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\test.ps1" -SkipFirmware
```

增加真实 FastAPI/SQLite 浏览器 E2E：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\test.ps1" -SkipFirmware -E2E
```

构建前端和固件：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build.ps1"
```

固件编译通过只证明源码能为配置的 `esp32cam` 目标生成产物。没有物理开发板、USB 串口握手和上传成功证据时，必须继续写“未进行真机刷写”。

## 8. 局域网访问

默认只监听 `127.0.0.1`。只有明确需要时才运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start.ps1" -Lan
```

打开电脑页面右上角或侧栏的“连接手机”：

1. 手机和电脑连接同一个家庭 Wi-Fi。
2. 用手机相机扫描页面二维码，选择在手机浏览器打开，无需安装 App。
3. 在电脑点击“查看访问码”，把这 8 位数字填到手机页面。访问码不写入二维码。

“手机查看物品”打开完整页面；“让这部手机可响铃”打开伴侣页。没有 LAN 地址时不生成假二维码，而是提示先启用局域网。地址可能因网络变化而变化，必要时点击“刷新地址”。localhost/127.0.0.1 不能用于另一台手机。

该机制面向可信家庭局域网，不包含 TLS，也不应映射到公网。手机打不开时先检查同一 Wi-Fi、访客网络隔离和专用网络防火墙规则，不要关闭整个防火墙。能在电脑访问 LAN 地址不等于已经验证用户实体手机可达。

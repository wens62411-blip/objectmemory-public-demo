# 物忆 ObjectMemory

本地运行的家庭物品视觉记忆原型：接入摄像头、登记物品照片、查看最后看到的位置，并用连续视频证据确认有意义的位置变化。包含 FastAPI、SQLite、React、手部分析、轻量三维场景和两种 ESP32 摄像头固件。

这是供体验与共同开发的版本。照片识别、手物交互和三维场景仍有局限；不能保证任意角度的手机都能被识别，也不能把“最后看到”当成“确认放下”。

## 干净下载，无个人数据

公开源码不包含原使用者的数据库、注册照片、摄像头画面、Wi-Fi 密码、设备令牌、USB 身份、运行日志或历史 Git 提交。首次真实启动没有物品或事件。演示内容由程序生成，使用单独的 DEMO 数据库，并在页面持续标记。

## Windows 快速体验

准备 Python 3.12、Node.js 22 或更新版，以及可用网络。首次双击 `Bootstrap.bat`，等依赖安装与前端构建结束。

| 入口 | 用途 |
| --- | --- |
| `Start.bat` | 真实模式，本机访问，初始空库 |
| `Start-No-Hardware-Demo.bat` | 生成测试视频并运行明确标记的虚拟设备演示 |
| `Start-Device.bat` | 真实模式，开启有访问码保护的局域网连接，用于 ESP32 接入 |

默认访问 <http://127.0.0.1:8018>。一次只启动一个上述入口；先退出当前服务再切换模式。详情见 [使用与协作指南](docs/PUBLIC_QUICK_START.md)。

照片匹配另需安装本地外观模型；未准备好时页面应显示具体缺失项：

```powershell
.\.venv\Scripts\python.exe scripts\prepare-appearance-model.py --install-runtime
```

## 插入开发板后的自动流程

支持 **Seeed XIAO ESP32S3 Sense** 和 **AI Thinker ESP32-CAM**，固件分板型构建。XIAO 使用 USB-C 数据线；AI Thinker 需要兼容的下载底板或 USB 串口适配器。

1. 用 `Start-Device.bat` 启动，在“设备中心”连接开发板。
2. 页面自动选择唯一可用端口，尝试填入电脑当前连接的 Wi-Fi 名称与后台候选地址；无法确定时明确提示。输入 2.4 GHz Wi-Fi 密码，核对板型并首次授权。
3. 后台自动准备缺失固件、检查产物哈希、读取芯片身份、固定该板并执行刷写、串口配网、注册和取帧检查。无需手动运行烧录工具。
4. 已成功刷入同版本的板再次连接会恢复连接，不反复擦写。失败时显示具体阶段；后台重启不会保存 Wi-Fi 明文密码，未完成的配网需要重新输入。

首次授权会覆盖目标板原程序。多串口、未知芯片或板型不匹配时不自动写入。编译成功、刷写成功、联网成功和收到视频帧分别显示，互不替代。

## 真实与演示的界限

- REAL、DEMO、TEST 分别使用独立数据库与媒体目录。REAL 无证据时显示“暂无真实位置记录”。
- 连续观察更新当前状态；只有通过连续性、位移、稳定与媒体校验的移动才保存历史事件。遮挡和丢帧不能直接判定放下。
- 默认 MINIMAL 每件物品保留最近两次未固定确认变化，固定事件和注册参考照片受到保护。
- ArUco 演示需要标签。无标签照片匹配是实验能力；合成视频演示不证明真实物品识别准确率。
- 三维场景是可编辑的轻量示意，不是相机自动还原的精确三维空间。语音入口受浏览器支持限制。

详见 [已知限制](docs/PUBLIC_KNOWN_LIMITATIONS.md)。

## 开发

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\dev.ps1
.\.venv\Scripts\python.exe -m pytest apps/api/tests services/vision/tests -q
cd apps\web
npm.cmd test
npm.cmd run build
npm.cmd run test:e2e:real
```

真实浏览器测试启动独立 FastAPI 与空 SQLite；硬件契约测试中的模拟串口不能作为真机验证。部分视觉测试需要额外下载模型或测试素材，缺失时跳过并说明原因。

| 目录 | 内容 |
| --- | --- |
| `apps/api` | API、数据库、设备管理、来源验证、存储清理 |
| `apps/web` | React 页面、设备引导、语音入口、三维场景 |
| `services/vision` | 取流、检测、跟踪、手物交互、事件与媒体 |
| `firmware/esp32cam` | 两种板型的固件源码 |
| `scripts` | 安装、启动、模型准备、构建与验证 |
| `tools/virtual_esp32cam` | 明确标记的虚拟设备 |

朋友可 Fork 后提交 Pull Request。公开可读不等于拥有仓库直接写权限。代码遵循 MIT；第三方模型和依赖遵循其各自许可，见 [许可说明](LICENSE_NOTICES.md)。

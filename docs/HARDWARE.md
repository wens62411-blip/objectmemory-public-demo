# ESP32-CAM 硬件

## 当前结论

2026-09-08 经用户明确授权，COM3 上的 XIAO ESP32S3 Sense 已完成一次固定版本真机刷写，成功时间 `2026-09-08T11:24:16.471470Z`。随后实际收到保存配置的 OMACK 和 `boot 0.1.0`，但多次 Wi-Fi `reason=202` 认证失败，120 秒内没有 OMREADY；安装任务失败，收据仍为 `flashed`、`linked_at=null`、已注册固件设备为 0。尚无 XIAO 截图、MJPEG、心跳或断电恢复验收。

09-05 仅检测到 Bluetooth COM5/COM6 是历史记录。09-08 真机刷写清单 SHA-256 为 `760ac5c4861541cb546573a349e06ae78045977ceb0d05707f2ee1bd23dffa3e`，固件 SHA-256 为 `eb5efa39e2ee5942a180e3d83f9920cff12560ca74a3eea6d226743f1b0d24a5`。编译清单与真实 USB 安装收据分开保存，编译本身不是刷写证据。

电脑热点已实测为 2.4 GHz、信道 11。实际 Arduino 2.0.17 SDK 开启 WPA3 SAE，不能断言“不支持 WPA3”，也不能仅凭 reason 202 断言密码错误。下一步在本地重新填写热点密码，使用“只重新配网”；已有成功刷写收据时不会重刷。密码不要发到聊天或日志。详见 [当前修复验收](CORE_VISION_3D_FIX_REPORT.md)。

REAL 中非模拟设备的网络 claim、一次性配对码领取、在线心跳、IP 地址或 MJPEG URL 同样不是物理硬件证明。当前后端把仅经网络 claim 的记录标为 `hardware_type=unverified`、`source_type=esp32_unverified`，不绑定摄像头；只有 REAL 主机本机的合格 USB 串口流程收到匹配 `OMREADY` 后才可升级为 `physical/esp32_real`。DEMO/TEST 中的模拟 claim 只形成 `virtual/virtual_esp32`，不能向上推断物理身份。

## 目标硬件

当前 `platformio.ini` 有两个独立目标，不能互刷：XIAO ESP32S3 Sense（`seeed_xiao_esp32s3`、ESP32-S3、8 MB Flash、独立引脚与分区），以及下列 AI Thinker 目标：

- AI Thinker ESP32-CAM 板型，PlatformIO board ID `esp32cam`；
- OV2640 摄像头及 AI Thinker 引脚映射；
- `espressif32 7.0.1`；
- Arduino ESP32 core 2.0.17；
- ArduinoJson 7.4.3；
- 115200 串口监视，460800 上传速度；
- JPEG、单帧 `/capture` 和独立 81 端口 MJPEG `/stream`。

用户当前的 **XIAO ESP32S3 Sense 使用可传数据的 USB-C 线直接连接电脑，不需要 AI Thinker 下载底板**。Sense 摄像头仍需实际初始化与出图验证，ROM 识别不能证明传感器型号。

仅针对 AI Thinker 的组合是 **AI Thinker ESP32-CAM + OV2640 + ESP32-CAM-MB 下载底板 + 数据线**；不要把其采购与接线要求套用到 XIAO，其他摄像头板也不能仅凭名称相近视为兼容。

数据线必须支持数据传输，不能只充电；接口要与实际 MB 底板一致：Micro-USB 底板配 Micro-USB 数据线，USB-C 底板配 USB-C 数据线，电脑端也需匹配。不要假设所有 MB 底板都是同一种插座，或看到 CH340 串口就认定 ESP32 主板已经可用。本页组合仅说明源码适配，不构成对具体在售套装、到手内容或商家的背书；全网最低价未独立核验。

另需稳定 5 V/至少 1 A 电源和 2.4 GHz Wi-Fi。摄像头与 Wi-Fi 同时工作时供电不足会引起复位和花屏；ESP32-CAM 不支持 5 GHz Wi-Fi。

使用裸板和 USB-TTL 时需按板卡丝印交叉连接 TX/RX、共地，并在刷写阶段让 GPIO0 接地后复位。此路径更易受供电、线序和下载模式影响；现场优先用 MB 底板。任何接线都应断电完成，不要带电反插板卡或摄像头排线。

## 固件职责

固件只做采集与设备协议，视觉推理留在 Windows 主机：

- 初始化 OV2640；
- 保存串口或备用热点配网配置到 NVS；
- 使用一次性配对码领取设备 token；网络 claim 只建立待验证记录；
- 每 15 秒提交带 Bearer token 的心跳；
- 提供健康、设备信息、JPEG 单帧和 MJPEG；
- 领取管理员排队的重启/恢复出厂命令。

因此当前不是“插上小板就能脱离电脑独立记忆物品”的成熟产品。Windows 主机仍必须持续运行取流、物品检测、跟踪、事件证据门、SQLite、媒体存储和页面服务；电脑或后台停止后，ESP32 不会接替这些功能。购买开发板只补足远端采集硬件，不会自动完成真实物品移动验收或产品化。

管理员会话与设备 token 分开。后台只保存设备 token 哈希；token 在领取响应中只返回一次。虚拟设备遵循同一协议，但始终标记 `hardware_type=virtual`、`source_type=virtual_esp32`。

非模拟设备从网络领取 token 后仍是“来源未验证设备”。后台不会仅凭设备自报字段、心跳或流地址为它创建 REAL 摄像头。claim 中的 `camera_id` 只用于后续串口核对，管理员列表不会把它作为可查看的摄像头链接返回。

## 固件 HTTP 接口

| 固件接口 | 当前行为 |
|---|---|
| `GET /health` | 在线时长、堆内存、RSSI、摄像头状态、版本 |
| `GET /device` | ID、名称、房间、IP/MAC、截图和视频 URL |
| `GET /capture` | OV2640 当前 JPEG |
| `GET /stream` | 80 端口跳转到 81 端口的 multipart MJPEG |
| `POST /config` | 只在 pairing mode 开放的备用 JSON 配网 |

未配置设备建立 `ObjectMemory-<后四位>` 开放式临时热点，失败约 30 秒后也可回退；须本次启动实际完成后台认证才关闭 AP，仅连上 Wi-Fi 不够。备用配网使用 HTTP、不提供 TLS，开放配网热点仍是安全边界。当前 `/device`、`/capture`、`/stream` 已要求独立 HMAC 派生读取凭据，不接受心跳 token 或查询参数凭据，也不再开放通配 CORS；`/health` 仍可无凭据读取状态。此保护尚未在本块 XIAO 上端到端验证，仍只适合可信隔离 LAN。不要同时从 AP 页面和 USB 提交配置，二者并发写配置的完整串行化尚待补齐。

## 编译

推荐使用项目入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build.ps1"
```

也可只构建固件：

```powershell
& ".\.venv\Scripts\python.exe" ".\scripts\firmware-build.py"
```

脚本使用独立 `.venv-firmware` 运行 PlatformIO，并在中文路径场景使用受控英文构建缓存。成功构建会生成 manifest、哈希和二进制产物。Retention 只保留最近两个成功构建并删除失败/更旧目录。

“编译通过”只表示指定工具链为当前目标生成产物。它不证明 USB 驱动、板卡型号、供电、摄像头排线、Flash 上传、Wi-Fi 或视频可用。本轮实际构建退出码和产物哈希见 [审计报告](AUDIT_REPORT.md)。

历史 AI Thinker 构建记录为 `2026-09-05T09:43:45Z`、35.59 秒、仅编译未刷写；不作为当前 XIAO 固件或硬件结果。当前 XIAO 产物见 [专用清单](../artifacts/firmware/xiao-esp32s3-sense/manifest.json)，真实刷写及未完成配网结果见 [物理安装记录](../data/verification/hardware-closure-20260908/physical-install.json)。`published=true` 仍只表示本地产物发布，实际刷写须另看 USB 收据。

## 物理刷写与绑定

硬件到手后，先在 Windows 设备管理器和设备中心确认插拔时新增了带 USB VID/PID 的串口。Bluetooth COM、没有 VID/PID 的虚拟口和调用方随意输入的端口会被拒绝。

USB flash、串口 provision 和完整 install 只允许在 REAL 进程执行。DEMO/TEST 只允许固件 build 和隔离的虚拟设备协议，不允许访问物理串口；设备页也会禁用面向物理设备的网络申领入口，后端对物理串口操作返回 409。若当前是 DEMO/TEST，必须先完整停止服务，再以 REAL 重启。

默认启动只监听 `127.0.0.1`，电脑上的网页可用不代表 ESP32 能连接。需要安装并配网时，先停止当前服务，再使用 `scripts/start.ps1 -Lan` 显式重启并使用管理员访问码。设备中心只在当前进程的实际监听套接字得到验证后开放需要局域网的操作；安装/配网在创建任务前及开始硬件步骤前还会复核后台 URL 是本机实际监听的 IP 和端口。纯编译、纯 USB 刷写不要求 LAN。

页面提供的 LAN URL 仍只是候选地址：本机监听成功不能证明 ESP32 已接入同一网络，也不能证明路由器隔离、防火墙或无线信号已经通过。设备真正收到匹配 `OMREADY` 并完成后续实测前，不得把候选地址描述成“设备已连接”。

以下命令仅适用于 AI Thinker，不能用于用户的 XIAO；XIAO 使用设备中心的专用板型、同板身份与固定版本绑定安装。已有成功收据但配网失败时选择“只重新配网”，不重新烧写。

AI Thinker 命令行刷写：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\flash-esp32cam.ps1" -Port COM7
```

必须把 `COM7` 替换为当次实际枚举的合格 USB 串口。脚本用参数数组调用固定构建器，不把前端文本拼成 shell 命令。

完整人工验收顺序：

1. 记录插板前后串口差异、VID/PID 和板卡照片；
2. PlatformIO 上传真实退出码为 0，并保存 esptool 芯片握手/Flash 日志；
3. 115200 串口发送一行 `OMCFG:{...}`，收到 `OMACK`；
4. 设备连接 2.4 GHz Wi-Fi，领取一次性 token；此时后台仍显示 `unverified/esp32_unverified`，不绑定摄像头；
5. REAL 主机从执行配网的同一本机合格 USB 串口收到 `OMREADY`，并核对 device、camera、IP、stream/capture URL 与待验证记录匹配；
6. 后台才显示 `physical/esp32_real` 而非 virtual/unverified，心跳持续；
7. 后台实际请求 `/health`、`/device`、`/capture` 并解码 JPEG；
8. 从 `:81/stream` 连续读取多帧且画面变化；
9. 断电、独立 5 V 重启后配置和绑定恢复；
10. 真机流进入 REAL，并另外完成物品事件证据门验收。

任何一步未执行都要单独写“未验证”。物理流成功也不自动证明物品识别成功。

## 串口协议

配置是一行 UTF-8 JSON，带前缀并以换行结束；固件限制单行长度。敏感值在日志中应脱敏。

```text
OMCFG:{"ssid":"...","password":"...","backend_url":"http://192.168.1.20:8018","pairing_code":"OM-******","device_id":"omcam-xxxx","device_name":"客厅摄像头","room_name":"客厅"}
OMACK:{"success":true,"device_id":"omcam-xxxx"}
OMREADY:{"device_id":"omcam-xxxx","camera_id":"cam_xxxx","ip":"192.168.1.20","stream_url":"http://192.168.1.20:81/stream","capture_url":"http://192.168.1.20/capture"}
```

设备看到的后端地址必须是 Windows 主机的局域网 IP，不能写设备自身无法访问的 `127.0.0.1`。

## 虚拟设备

虚拟设备只允许在 DEMO/TEST 启动。使用完整无硬件入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-demo-no-hardware.ps1"
```

它可以验证领取、token、心跳、JPEG/MJPEG、命令和设备页面，但不验证 USB、Flash、OV2640、供电或无线射频。不得在 REAL 中运行，也不得把“虚拟在线”描述为“物理摄像头在线”。DEMO/TEST 虚拟协议不会获得物理串口操作权限；如需刷写或串口配网，必须退出当前模式并重启到 REAL。

## OTA

当前 OTA 路由明确返回未支持。仓库没有完成签名下载、分区回滚、断电恢复和真机升级验收；首次安装和更新继续使用受控 USB 刷写。页面不得显示 OTA 成功。

## 官方参考

- [PlatformIO AI Thinker ESP32-CAM 板卡](https://docs.platformio.org/en/latest/boards/espressif32/esp32cam.html)
- [Arduino ESP32 Preferences/NVS](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/preferences.html)
- [Espressif CameraWebServer 示例](https://github.com/espressif/arduino-esp32/tree/master/libraries/ESP32/examples/Camera/CameraWebServer)

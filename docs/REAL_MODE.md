# REAL 模式

REAL 是默认且唯一可承载真实家庭位置证据的模式。它的原则是 fail closed：没有满足证据门的记录时显示“暂无”，而不是用演示答案填空。

## 启动

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start.ps1" -Port 8018
```

`Start.bat` 使用同一入口。脚本强制 `OM_RUNTIME_MODE=REAL`，默认监听 `127.0.0.1`。端口被占用时使用其他 `-Port`；只有明确要在可信家庭局域网访问时才追加 `-Lan`。

启动后核对：

```powershell
$null = Invoke-RestMethod http://127.0.0.1:8018/api/session -SessionVariable objectMemorySession
Invoke-RestMethod http://127.0.0.1:8018/api/runtime-config -WebSession $objectMemorySession
Invoke-RestMethod http://127.0.0.1:8018/api/system/diagnostics -WebSession $objectMemorySession
```

响应应显示 `runtime_mode=REAL`、`is_simulated=false`，数据库为 `data/database/objectmemory.sqlite`，媒体目录为 `data/real-media`。

先在本机建立会话；首行不输出会话中的局域网访问码。直接无 Cookie 调用受保护接口会返回 401，不能据此判断后端未启动。

## REAL 明确禁止

- 自动执行 demo seed；
- 自动加载或上传测试视频；
- 启动或注册虚拟 ESP32；
- 返回固定的“手机在桌面/沙发”；
- 接受 `video_file`、`virtual_esp32`、`demo_seed`、`test_fixture` 或 `mock` 事件；
- 把浏览器上传 JPEG 当成已验证物理摄像头，或据此生成确认放置事件；
- 把 ESP32 网络 claim、普通在线心跳或视频 URL 当成物理板证明；
- 把来源未知的旧事件作为默认搜索证据；
- 把断流、重连、追踪器重置或单帧检测当成移动。

后端在配置创建、设备注册、事件写入和查询等多层执行模式检查。前端隐藏 DEMO 操作只是辅助，不是安全边界。

## 空数据库的正确表现

没有摄像头或没有满足证据门的事件时：

- 历史事件数为 0；
- `item_current_state` 可以为空；
- 截图和录像目录不应出现事件媒体；
- 搜索不猜测位置，并显示“暂时没有真实摄像头产生的位置记录。”；
- 页面刷新、WebSocket 重连和后端重启不会改变事件数。

第一次创建规范 REAL 数据库时，迁移只接受旧库中的非演示物品、注册参考图、设置及非模拟摄像头/区域；不导入旧事件。旧的混合数据库仍由审计脚本分类，不能默认信任。

## 允许的来源

| `source_type` | 来源 | 附加条件 |
|---|---|---|
| `opencv_camera` | 本机 OpenCV 摄像头 | 持续变化帧、来源会话有效 |
| `browser_camera` | 浏览器上传帧 | 只能更新“最后看到”；来源未验证，不能确认放置 |
| `rtsp` | 网络视频流（物理来源未验证） | 只能更新“最后看到”；不能确认放置 |
| `onvif` | 网络视频流（物理来源未验证） | 只能更新“最后看到”；不能确认放置 |
| `mjpeg` | 网络视频流（物理来源未验证） | 只能更新“最后看到”；不能确认放置 |
| `esp32_real` | 已验证物理 ESP32-CAM | REAL 本机合格 USB 串口 + 匹配 `OMREADY` + 真实流 |
| `authorized_screen_capture` | 用户授权的屏幕固定矩形 | 本机、短期授权、选择区域 |

来源被允许也不等于事件被确认。每个自动 movement 还要满足连续帧、稳定前态、有效位移、新位置稳定、媒体可解码、哈希匹配和幂等检查。

`browser_camera` 是特殊的 fail-closed 来源：会话、Origin、WebSocket 和 JPEG 校验只能证明后台收到了当前浏览器连接发送的帧，不能证明帧来自用户声称的物理摄像头。REAL 可以保留 `observation_only` 当前状态并显示“最后看到 / 浏览器帧（来源未验证）”，但不得把它升级为 `confirmed placed`、真实确认位置或可搜索的精确放置证据。

RTSP、ONVIF 和 MJPEG 是管理员配置的视频流，不是硬件身份证明。当前 REAL 后端对它们采用 fail-closed：可以更新来源未验证的“最后看到”，但不能生成 `confirmed placed`、真实确认位置或默认搜索中的精确放置证据。将来只有引入设备证明并完成受监督验收后，才可单独放开某个来源。

## ESP32 物理身份

设备通过一次性配对码从网络发起 claim 时，后台只保存 `hardware_type=unverified`、`source_type=esp32_unverified` 的待验证记录；该记录没有摄像头绑定，在线心跳也不会把它升级为物理设备。只有 REAL 进程在运行主机本机发现合格 USB 串口、完成受控配网，并从该串口收到与 claim 设备和地址匹配的 `OMREADY`，才会绑定摄像头并标记为 `esp32_real`。

固件 build 在各模式可独立验证工具链，但 USB flash、串口 provision 和 install 只允许在 REAL。DEMO/TEST 的设备页还会禁用面向物理设备的网络申领入口，后端对物理串口操作返回 409；隔离的虚拟 ESP32 仍可在 DEMO/TEST 内走模拟配对。切换到物理流程时必须停止当前服务并以 REAL 重启，不能在同一进程触碰物理串口。

## 搜索与当前状态

原位置的持续观察只更新 `item_current_state.last_seen_at`、置信度和必要坐标，不创建新历史事件或媒体。默认搜索只返回：

- REAL 模式；
- 非模拟来源；
- `evidence_status=confirmed` 的完整 movement，或明确的人工纠正；
- 来源字段不为 unknown/mock/demo/test/video/virtual。

遮挡或暂时离开视野时，界面应显示“最后看到”或“被遮挡”及时间。只有在新位置重新稳定后才允许“确认放下”。

## 安全访问

本机默认会话不面向公网。`-Lan` 会绑定 `0.0.0.0` 并要求管理员访问码换取 Cookie；设备 token 不能调用管理员管理接口。当前没有 TLS，请勿把端口映射到互联网。

## 停止与切换

在启动窗口按 `Ctrl+C`。正常关闭会停止采集、释放摄像头、停止媒体工作线程并做一次轻量 retention。切换到 DEMO/TEST 前必须完全停止 REAL 进程；不要通过修改请求参数尝试切换数据库。

## 当前验收边界

当前规范 REAL 数据库完整性检查为 `ok`，只有 1 条 webcam 配置，`items=0`、`movement_events=0`、`item_current_state=0`、`event_media=0`、`tracks=0`；配置存在不代表设备正在运行。REAL 无源 E2E 和两代重启均保持 0 事件，seed/virtual 请求被拒绝，规范数据库哈希不变。

本轮物理电脑摄像头正式 30 分钟取帧、浏览器 JPEG/WebSocket 软件通道、队列、内存门和停止释放已验证，但浏览器通道仍属于来源未验证观察；当次 0 ArUco、0 真实确认事件。没有使用真实物品/ArUco 标签完成跨区 movement，因此真实识别、真实截图/短片和真实确认位置仍未通过人工场景验收。具体证据见 [审计报告](AUDIT_REPORT.md)。

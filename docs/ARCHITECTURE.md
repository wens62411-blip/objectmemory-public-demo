# 当前架构

本文描述本仓库当前实现，不把页面文案、演示回放、单元测试或固件编译当成真实硬件证据。

## 运行边界

ObjectMemory 是一个本机 FastAPI 进程承载 React 静态页面、SQLite 和视觉工作线程的单机系统。`OM_RUNTIME_MODE` 在应用创建时解析为 `REAL`、`DEMO` 或 `TEST`；模式决定数据库、媒体目录和允许的数据来源。进程运行期间没有热切换入口，切换必须停止并重新启动。

| 模式 | 数据库 | 媒体 | 允许的模拟来源 |
|---|---|---|---|
| `REAL` | `data/database/objectmemory.sqlite` | `data/real-media` | 无 |
| `DEMO` | `data/database/objectmemory-demo.sqlite` | `data/demo-media` | `video_file`、`virtual_esp32`、`demo_seed` |
| `TEST` | 测试数据根中的 `database/objectmemory-test.sqlite` | 测试数据根中的 `test-media` | `test_fixture` 及受控测试来源 |

日志和临时文件也按模式分到 `data/logs/<mode>`、`data/temporary/<mode>`。注册物品原始参考图单独位于 `data/registered-items`，不属于普通 retention 的删除目标。

REAL 查询不仅限定 `runtime_mode=REAL`，还排除 `is_simulated=true` 以及 `unknown`、`mock`、`demo_seed`、`test_fixture`、`video_file`、`virtual_esp32`。这种双重过滤用于应对旧数据或错误调用方写入的脏字段。

## 数据流

```text
CameraSource
  -> CaptureWorker / 有界最新帧队列
  -> Detector（ArUco 或实验检测器）
  -> ItemIdentity（标签 ID 或参考图匹配）
  -> StableTracker
  -> ZoneManager
  -> MovementStateMachine
  -> 有界内存环形缓冲
  -> EventService / EvidenceGate
  -> EventService 媒体写入与来源绑定
  -> SQLite
  -> Search API / WebSocket / React
```

每一层都携带 `source_session_id`、帧号、时间戳、来源类别和模拟标记。诊断模式会额外暴露检测框、轨迹 ID、区域、速度、状态机状态、候选拒绝原因和媒体路径；普通页面只呈现用户需要的来源与证据状态。

### 1. 取流

`services/vision/camera_sources` 实现电脑摄像头、浏览器推帧、视频文件、RTSP、ONVIF、MJPEG/ESP32 和经授权的屏幕矩形。每个启动或重连周期形成新的来源会话。队列满时丢弃旧帧，避免用积压旧画面产生迟到事件；停止时释放 `VideoCapture` 和相关线程。

同一物理摄像头索引由租约保护，页面订阅者共享后端源，不应各自重开摄像头。浏览器摄像头是例外：像素由浏览器取得后通过有会话与来源校验的 WebSocket 推入既有后端会话。

预览是取流后的独立支路：CaptureWorker 以单槽发布最新原始帧，VisionEngine 的一个预览线程编码新帧并生成原子快照（JPEG、源帧号、会话、时间戳、预览序号）。MJPEG 的所有订阅者复用此快照，只有序号改变才发送。识别仍按独立频率消费最多 2 帧的有界队列；事件媒体不从前端预览反向生成。`capture_fps`、`preview_fps`、`inference_fps` 是分别测量的速率，旧 `fps` 仅兼容推理速率，目标速率另列。

### 2. 检测、识别和跟踪

ArUco `DICT_4X4_50` 是显式标签路径，ID 0–49 由注册信息映射身份。照片实验模式组合 NanoDet 候选、DINOv2-small 参考特征与连续观察，不会在失败时静默回退 ArUco。手部分析与关键点显示独立；持续手身份、几何姿势、手物共同位移与可见释放附带来源会话/帧证明。它们仍缺少有标注的真实家庭场景准确率基准，不应称作成熟的“无标签 AI”。详见 [照片识别](PHOTO_MEMORY_GUIDE.md) 与 [手部动作](HAND_ACTION_GUIDE.md)。

跟踪器把逐帧检测整理成稳定轨迹；区域管理器把归一化坐标映射到用户配置的多边形。短暂丢检、抖动和手从旁边经过只改变候选状态，不直接写历史事件。

### 3. 状态机和证据门

状态机按“稳定基线 → 有意义移动 → 新位置重新稳定”形成一个 movement episode。跨区域或同一区域内超过阈值的位移才有资格进入证据门。帧间长断流、摄像头重连、视频回绕和追踪器重置会清空基线，不能被解释为移动。

证据门在写库前再次核验：

- 摄像头与物品存在，来源与当前运行模式一致；
- `movement_session_id`、`source_session_id`、帧范围和时间范围存在；
- 帧连续、没有重连，移动前后都稳定；
- 起点和终点有意义地不同；
- 拿起和重新放稳的证据帧落在事件帧范围内；
- 置信度达到配置门槛，检测器、跟踪器和检测模式位于允许清单；
- 主截图可解码，短视频可读取至少一帧；
- 媒体由 EventService 从本次回调内存帧写出，数据库 SHA-256、来源 binding 及 binding digest 与实际文件一致；
- 同一 movement 的幂等键尚未存在。

不满足条件的候选保留在内存诊断状态，不包装成 `confirmed_placed`。没有在新位置重新检测并稳定时，当前状态只能表示最后看到、遮挡或丢失。

## 两层事件模型

### `item_current_state`

每个物品在当前模式只保留一条状态，包括当前摄像头、房间、区域、位置、`last_seen_at`、最近确认放稳时间、可信度、来源和证据事件 ID。物品在原位置持续出现时只更新这条记录，不创建历史行，也不新存截图或录像。

### `movement_events`

一条记录代表一次完整 movement episode 或一次明确的人工纠正。它保存运动会话、来源会话、帧与时间范围、起止区域、最终位置、置信度、检测/跟踪后端、证据状态、截图/录像路径和哈希、创建者、固定与人工纠正标记。兼容 API 仍可能使用 `events` 作为名称，但权威存储表是 `movement_events`。

`event_media` 登记媒体路径、种类、哈希、来源会话和删除状态。`source_sessions` 登记来源启动、重连与停止边界。事件 ID 和幂等键有唯一约束；连续观察通过 `item_current_state` 合并。

## 存储与并发

SQLite 使用 WAL、5 秒 busy timeout 和有限指数退避重试。事件媒体先写临时文件，完成后原子替换到当前模式的媒体目录；随后 movement、两条媒体登记和当前状态在同一 SQLite bundle 事务中提交。事务失败或并发唯一键竞争的输家会补偿删除本次随机媒体。这里是“文件原子替换 + 数据库事务 + 失败补偿”，不是把文件系统和 SQLite 冒充成一个跨系统事务。媒体任务和前事件视频环形缓冲都有上限；持续视频不长期落盘。

`RetentionService` 在启动、确认事件后、每 30 分钟、用户手动清理和正常退出时运行。默认 `MINIMAL` 每件物品保留最近 2 个未固定确认移动事件，同时保护 `pinned` 和当前状态所引用的证据。删除采用“数据库标记 `pending_delete` → 提交 → 删除受限目录内文件 → 删除/更新媒体记录”的恢复式流程。

详见 [存储保留策略](STORAGE_RETENTION_POLICY.md)。

## Web 和接口

FastAPI 同时提供 REST、媒体读取、WebSocket 和生产前端静态文件。关键事件、搜索、设备和存储响应包含或继承 `runtime_mode`、`source_type`、`is_simulated`；前端以这些显式字段显示“真实摄像头 / 测试视频 / 虚拟设备 / 演示种子 / 人工纠正”，不再根据名字猜测来源。

REAL 无可信位置时，搜索返回空证据和“暂时没有真实摄像头产生的位置记录。”。前端请求失败会清除旧结果，不能用上一次结果填充当前查询。

## 安全边界

- 默认监听 `127.0.0.1`；只有显式 `-Lan` 才绑定局域网。
- 本机使用会话 Cookie；LAN 模式使用启动时生成的管理员访问码换取会话。
- 管理员会话与设备 token 分离，数据库只保存设备 token 的哈希。
- Origin、会话、上传类型/大小和媒体路径均受检查。
- 网络摄像头仅接受字面量私有、回环或链路本地 IP，降低 SSRF 风险。
- 屏幕采集只允许用户当次授权的固定矩形，授权有过期时间。

这仍是可信家庭局域网级边界：没有 TLS、企业身份管理、磁盘加密或公网部署防护。ESP32 配网回退 AP 和固件 HTTP/CORS 还有单独限制，见 [已知限制](KNOWN_LIMITATIONS.md)。

## 关键目录

| 目录 | 当前职责 |
|---|---|
| `apps/api/app` | 运行模式、数据库、事件证据门、搜索、存储、设备与认证 |
| `apps/web/src` | React 页面、模式提示、来源标签、浏览器摄像头与存储管理 |
| `services/vision` | 视频源、检测、跟踪、区域、状态机、媒体缓冲 |
| `firmware/esp32cam` | AI Thinker ESP32-CAM 固件源码 |
| `tools/virtual_esp32cam` | DEMO/TEST 虚拟设备协议实现 |
| `scripts` | 安装、启动、测试、诊断、审计和清理入口 |
| `audit` | 机器可读的本轮前后快照、分类和测试结果 |

## 当前证据等级

架构存在不等于端到端已经真实验证。本轮可证明物理电脑摄像头能产生变化帧并正确释放；尚未用真实标签物品完成跨区移动并生成确认事件。测试视频端到端只能证明 DEMO 链路。固件编译只能证明源码可构建；没有物理 ESP32-CAM 检测或真机刷写证据。

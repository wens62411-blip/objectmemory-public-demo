# 数据来源与证据链

## 原则

ObjectMemory 不根据摄像头名称、物品名称、页面颜色或媒体文件存在来判断真实性。真实性由运行模式、来源类型、来源会话、帧/时间范围、证据状态和文件哈希共同决定。

一张可打开的截图只能证明“存在这个文件”。它不能单独证明画面来自物理摄像头、属于该物品、覆盖该事件时间或未由测试回放生成。

## 运行模式

| 模式 | 意义 | `is_simulated` 的正常值 |
|---|---|---|
| `REAL` | 可承载真实家庭摄像头证据 | `false` |
| `DEMO` | 合成视频、虚拟设备和演示配置 | `true` |
| `TEST` | 自动化 fixture 和隔离测试 | `true` |

模式在服务创建时确定。事件、摄像头、轨迹、当前状态、媒体和来源会话都带 `runtime_mode`，并由独立数据库与媒体目录进一步隔离。

结构化 API 响应显式返回 `runtime_mode`、`source_type`、`is_simulated`。JPEG、MJPEG 和 ZIP 等二进制响应不能携带 JSON 字段，因此使用等价的 `X-ObjectMemory-Runtime-Mode`、`X-ObjectMemory-Source-Type`、`X-ObjectMemory-Is-Simulated` 响应头；前端不得丢弃这些来源信息后再把内容统一标成“AI 识别”。

## 来源类型

| `source_type` | 用户界面 | 模拟 | 能否进入 REAL 确认事件 |
|---|---|---:|---:|
| `opencv_camera` | 本机 OpenCV 来源 | 否（本机接口） | 可以，仍需证据门；物理镜头须现场确认 |
| `browser_camera` | 浏览器帧（来源未验证） | 否（声明值） | 不可以；REAL 中只允许“最后看到”观察 |
| `rtsp` | 网络视频流（物理来源未验证） | 否（声明值） | 不可以；REAL 中只允许“最后看到”观察 |
| `onvif` | 网络视频流（物理来源未验证） | 否（声明值） | 不可以；REAL 中只允许“最后看到”观察 |
| `mjpeg` | 网络视频流（物理来源未验证） | 否（声明值） | 不可以；REAL 中只允许“最后看到”观察 |
| `esp32_unverified` | 来源未验证设备 | 未知 | 不可以；网络 claim 不绑定摄像头 |
| `esp32_real` | 已验证物理 ESP32-CAM | 否 | 可以；必须由 REAL 本机合格 USB 串口和匹配 `OMREADY` 建立 |
| `authorized_screen_capture` | 授权窗口采集 | 否 | 可以，但只限用户选定矩形 |
| `video_file` | 测试视频 | 是 | 不可以 |
| `virtual_esp32` | 虚拟设备 | 是 | 不可以 |
| `demo_seed` | 演示种子 | 是 | 不可以；seed 本身不应生成 movement |
| `test_fixture` | 测试 fixture | 是 | 不可以 |
| `mock` | Mock | 是 | 不可以 |
| `unknown` | 来源未验证 | 未知 | 不可以 |

`webcam`、`browser`、`screen`、`video`、`esp32` 是摄像头配置类型，不是可由客户端自行声明的真实性等级。`browser` 上传的是客户端 JPEG；服务端无法证明这些字节确实来自 `getUserMedia` 或某个物理镜头，因此即使记录为 `REAL/browser_camera/false`，也只能更新“最后看到”，不得成为确认放置或 `real_verified`。RTSP、ONVIF 和 MJPEG 同样只表示管理员配置并通过地址安全校验的视频流 URL；当前没有设备证明或受监督验收把网络字节绑定到具体物理镜头，所以 REAL 中只允许更新“最后看到”，不能生成确认 movement。

REAL 中非模拟 ESP32 的网络 claim 只创建 `hardware_type=unverified`、`source_type=esp32_unverified` 的待验证记录，不自动创建或绑定摄像头，也不能生成 REAL 确认事件。只有 REAL 进程在本机枚举到合格 USB 串口，完成受控刷写/串口配网，并从同一串口收到与待验证记录匹配的 `OMREADY` 后，才会升级为 `hardware_type=physical`、`source_type=esp32_real`。DEMO/TEST 内部运行的模拟 ESP32 始终归一化为 `virtual_esp32`，只能绑定隔离的模拟摄像头。

## 来源会话

每次摄像头启动、视频回绕或断开后重连都形成新的 `source_session_id`。`source_sessions` 至少记录：

- `camera_id`；
- `runtime_mode`、`source_type`、`is_simulated`；
- 开始、结束与最近帧时间；
- 首末帧编号；
- `continuity_ok` 和状态；
- 诊断元数据。

状态机只能在同一连续来源会话内形成 movement。重连、回绕、帧号倒退或长断流会丢弃进行中的候选并重新建立稳定基线。

## 当前状态与历史事件

### `item_current_state`

每个物品、每种运行模式只有一行，包含：

- `item_id`；
- 当前摄像头、房间、区域和归一化位置；
- `last_seen_at`、`last_confirmed_placed_at`；
- 置信度、状态；
- `evidence_event_id`；
- `runtime_mode`、`source_type`、`is_simulated`、`source_session_id`。

同一位置的重复观察只更新这行。`last_seen` 不等于 `confirmed_placed`；遮挡或丢失不会猜测新位置。

### `movement_events`

完整 movement 或人工纠正包含以下 provenance 字段：

| 组 | 字段 |
|---|---|
| 标识 | `event_id`、`movement_session_id`、`idempotency_key`、`item_id`、`camera_id` |
| 模式与来源 | `runtime_mode`、`source_type`、`is_simulated`、`source_session_id` |
| 原始帧 | `source_frame_start`、`source_frame_end`、`source_timestamp_start`、`source_timestamp_end`、`ingested_at` |
| 算法 | `detector_backend`、`tracker_backend`、`detection_mode` |
| 变化 | `from_zone`、`to_zone`、`final_position`、`confidence`、`final_status` |
| 阶段证据 | `pickup_evidence`、`placement_evidence` |
| 媒体 | `before_screenshot`、`after_screenshot`、`screenshot_path`、`screenshot_sha256`、`clip_path`、`clip_sha256` |
| 人工与保留 | `created_by`、`pinned`、`manually_corrected`、`human_review_status`、`notes` |

自动写入路径由 EventService 独占：视觉层只交付本次回调的内存关键帧和有界 clip frames，不能自带既有路径或哈希。普通事件仍使用一张左右合成主图，不把同一路径计为两张截图。专用 `aruco_screen_validation` 使用 `event_service_atomic_v2`，必须保存不同路径的 before、after 和 clip 三份媒体，并在同一数据库事务中保存事件、当前状态和验收通过结果。

P0 录像强制覆盖移动前后各至少 5 秒，来源帧/时间均在同一 source session 内。写入后完整解码并比较帧数、每帧像素误差；对可检测的目标 ArUco 重新核对原始/编码帧中的唯一 ID 和位置，前后关键帧必须与轨迹端点一致。仅比较整幅图平均误差不足以证明小目标移动，不能单独作为绑定依据。压缩误差容许值集中定义在 EventService，像素核对不是摄像头硬件的加密证明。

### `event_media`

媒体注册表保存事件 ID、模式、来源会话、受限 URL、种类、SHA-256、事件时间元数据、`active/pending_delete` 状态和导出标记。自动事件还保存 `event_service_atomic_v1` write binding：`capture_origin=vision_callback`，并绑定 mode、source、session、帧/时间范围、输入帧哈希、输出媒体哈希和 `binding_sha256`。媒体 URL 只能解析到当前模式媒体根下的 `event-images`、`event-clips` 或 `thumbnails`。

P0 的 v2 额外记录 before/after 索引、独立哈希、录像来源首尾时间和帧号、实际编码帧率、原始帧哈希列表与编码后 ArUco 位置核验。套件冻结物品、标记、摄像头配置摘要、区域及阈值；普通 URL 参数不能重排 14 个试验、重试替换失败或提交成功结果。来源/config 改变必须新建套件。

套件汇总会重新读取保留的三份媒体并核对 SHA-256。若文件意外消失、被篡改或引用不一致，该轮不计成功。仅由受控 retention 在删除前验证完整、删除完成后登记的回执可以保留历史计分；它明确标记 `policy_deleted/evidence_retained=false`，不声称文件仍存在。回执与数据库都由本机应用管理，不抵抗拥有本机数据库写权限的恶意管理员。

## 真实事件证据门

自动 movement 写入前必须同时满足：

1. 物品和摄像头均存在；
2. 摄像头来源被当前模式允许，调用方不能覆盖来源；
3. 有稳定唯一的 movement session；
4. 有来源会话、合法帧范围和递增时间范围；
5. 帧连续且没有跨重连 epoch；
6. 多个连续帧检测到物品，并先形成稳定基线；
7. 跨区达到跨区阈值，或同区达到更大的同区位移阈值；
8. 物品在新位置重新稳定；
9. 拿起和放稳摘要的关键帧落在事件范围；
10. 置信度达到配置门槛，detector、tracker、detection mode 位于允许清单；
11. 主截图存在且 OpenCV 可解码；
12. 短片存在且能读出视频帧；
13. EventService 对刚写出的文件计算 SHA-256，视觉调用方不得提供路径或哈希；
14. 截图、短片、事件属于同一来源会话，write binding 及其 digest 完整一致；
15. 幂等键没有对应的既有事件。

这里的“来源被允许”还包含来源等级限制：`browser_camera`、RTSP、ONVIF、MJPEG 不进入自动确认 movement，网络 claim 形成的 `esp32_unverified` 不绑定摄像头。它们最多证明后台收到了某个连接中的图像字节，不能被页面改写成“物理硬件已验证”或真实确认位置。

人工纠正属于显式用户行为，只能通过已认证管理员入口 `/api/events/{id}/correct` 执行，并必须引用当前模式已有的确认事件。原截图以及存在时的短片必须仍有 active 媒体登记且实际哈希一致；新记录使用 `manual_correction_reference_v1` binding。离线审计器还会复核被引用原事件本身满足完整可信证据合同。人工纠正记录为 `created_by=user`、`manually_corrected=true`、`human_review_status=human_corrected`；它不是摄像头算法确认，页面必须显示“人工纠正”。

## 幂等键

幂等判断组合物品、摄像头、来源会话、movement session、起止区域、帧范围和关键截图哈希。SQLite 对 `event_id` 和 `idempotency_key` 都有唯一索引。这样可以阻断重复 HTTP 提交、WebSocket 重连、事件回调重试和同轮视频帧重复保存。

视频循环的新一轮使用新来源会话，因此可以作为新的 DEMO session 被审计；同一轮内同一 movement 不得重复。

## 搜索可信度

`confidence` 不是经真实家庭样本校准的正确概率。当前 ArUco 后端在成功解码候选后使用固定基数加尺寸项计算启发式分数，因此单独一个 0.93 不能证明目标真实存在或“93% 正确”。普通持久化观察还必须有当前服务端引擎的稳定基线证明；确认移动另有完整轨迹/媒体门。首次长跑已实际暴露单帧候选误入 last_seen，失败证据单列，不能仅凭高分掩盖。

REAL 搜索默认只看 REAL、非模拟、来源可识别的记录。以下记录不会被包装成真实答案：

- DEMO/TEST 数据库中的事件；
- 旧库中的 seed、virtual、test、mock 或 video 事件；
- `source_type=unknown` 或字段缺失的旧事件；
- `browser_camera` 声称的 confirmed/placed 记录，以及 `esp32_unverified` 网络申领记录；
- 没有可验证媒体却声称确认放下的事件；
- 未重新稳定的遮挡/最后看到状态。

无合格证据时统一返回“暂时没有真实摄像头产生的位置记录。”。管理员审计输出仍可保留 unknown 分类以便人工判断；`real_verified` 只有在模式、来源、帧/会话、pipeline、媒体、binding、哈希和人工引用（如有）的完整合同全部通过后才成立，unknown 不会凭名字或媒体存在升级。

## 证据等级

| 等级 | 可以证明 | 不可以证明 |
|---|---|---|
| 单元 Mock | 组件/函数在受控输入下工作 | 真实 API、数据库、摄像头 |
| 真实 FastAPI + TEST SQLite | 实际接口、事务、模式隔离 | 物理摄像头 |
| DEMO 文件视频端到端 | 视频文件经过完整软件链并生成媒体 | 家庭摄像头识别 |
| 本机 OpenCV 取帧 | 当前设备索引能够输出帧、后端处理和释放可用 | 该索引是否对应物理镜头、物品身份或 movement |
| 真实标签移动端到端 | 当次摄像头、标签、区域和事件证据通过 | 无标签识别能力 |
| 固件编译 | 源码能生成目标产物 | 开发板存在或刷写成功 |
| 虚拟设备协议 | 注册/心跳/流协议的软件路径 | 物理 ESP32 在线 |
| 真机串口、刷写、取流 | 指定物理板的当次结果 | 其他型号或长期稳定性 |

所有报告必须说明使用的是哪一级，不得向上推断。

## 本轮证据等级应用

本轮自动化验收的命令、测试数量、退出状态、失败项及修复后重跑结果，以 [`audit/p0-real/final-test-results.json`](../audit/p0-real/final-test-results.json) 为统一入口，不能沿用初次审计的计数、媒体字节数或耗时。结果必须分开读取：

- 前端单元与 Mock 组件／浏览器契约只证明受控响应下的界面行为。
- 真实 FastAPI、独立 SQLite、真实 WebSocket 和 REAL 浏览器 E2E 证明实际软件接口与无源时的行为；没有现场移动就不计为物理物品验收。
- DEMO 文件视频经过真实检测、状态机、媒体、数据库和搜索仍属于 `DEMO/video_file/true`；不能升级为家庭摄像头证据。
- 本机 OpenCV 取帧与稳定性检查只证明该数字设备索引的当次采集链。操作系统虚拟摄像头也可能提供该索引，必须由现场人员对照画面确认物理镜头身份；不得单凭取帧数量称物理身份已验证。
- 虚拟设备验证注册、令牌、心跳和视频协议；固件编译验证源码及产物，两者均不证明真机刷写或真实 ESP32 已上线。

当前现场 14 场景套件仍为 `NOT_RUN`：10 次实际物品跨区移动和 4 类负样本均尚未由现场人员完成。详情见 [`acceptance-runs.json`](../audit/p0-real/acceptance-runs.json) 与 [`accuracy-summary.json`](../audit/p0-real/accuracy-summary.json)。自动化回归通过不改变这一状态，不能宣称 P0 真实物品移动验收完成。

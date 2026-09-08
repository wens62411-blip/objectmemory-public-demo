# 真实事件验收标准

## 什么才是“真实确认”

真实确认事件必须同时属于 `REAL`、来源非模拟、经过连续视频帧的检测与跟踪、形成完整 movement episode，并且数据库、截图和短片能追溯到同一来源会话。接口被调用、文件存在或页面显示“在线”都不够。

## 自动 movement 的最低门槛

普通 movement 路径的默认阈值由设置模型统一配置；P0 专项套件另使用服务端冻结的独立合同和阈值，见 [P0 实物移动专项验收](P0_REAL_MOVEMENT_ACCEPTANCE.md)：

| 设置 | 默认值 | 作用 |
|---|---:|---|
| `min_detection_frames` | 3 | 形成候选所需连续检测帧 |
| `min_stable_frames` | 5 | 新旧位置稳定帧数 |
| `max_frame_gap_seconds` | 0.75 | 最大连续帧时间间隔 |
| `min_move_distance` | 0.04 | 跨区归一化最小位移 |
| `same_zone_move_distance` | 0.10 | 同区位置变化阈值 |
| `min_confidence` | 0.55 | 最低检测置信度 |
| `stable_speed` | 0.025 | 判定静止的最大归一化速度/秒 |
| `stable_position_jitter` | 0.018 | 稳定点最大抖动 |
| `min_stable_seconds` | 1.8 秒 | 形成稳定基线/放稳所需时间 |
| `movement_speed` | 0.035 | 启动移动候选的速度门槛 |
| `occluded_seconds` | 0.6 秒 | 遮挡状态门槛 |
| `lost_seconds` | 8 秒 | 丢失状态门槛 |

同区阈值必须不小于跨区阈值，丢失时间必须不小于遮挡时间。降低这些阈值后必须重跑全部场景，不能以“页面更容易出事件”作为正确性依据。

证据门还要求 movement/source session、合法帧和时间范围、无断流/重连、起止区有意义差异、移动前后阶段证据、最低可信度、pipeline 允许清单、可解码截图与短片、匹配 SHA-256、EventService 原子来源 binding 和唯一幂等键。movement、媒体登记和当前状态必须作为一个 SQLite bundle 提交，任一步失败都不得留下半成品记录或文件。

## 验收前准备

1. 新建隔离的 REAL 数据根或确认规范 REAL 数据库为空。
2. 核对 `/api/runtime-config` 是 `REAL/false`。
3. 禁止启动虚拟 ESP32、测试视频和 demo seed。
4. 固定摄像头，配置不重叠区域，记录物品与 ArUco ID。
5. 启用开发诊断模式时保存帧号、track ID、区域、状态机、拒绝原因和来源会话；完成后关闭。
6. 记录测试前数据库和媒体数量。

手机 Marker 地址由 preflight 使用当前服务实际监听的已验证 LAN 候选生成；它不是手机已可达的证明。手机显示页只需 `item`、`camera`，无 suite/run 也可显示已绑定标记；无二维码时使用链接并输入主机访问码。准备物品、标记与区域不等于执行验收，禁止为了生成手机链接提前创建或启动 14 轮。

## 必须实际执行的十个场景

### 1. 无视频源

REAL 不启动任何摄像头。事件数必须为 0，搜索手机显示“暂时没有真实摄像头产生的位置记录。”，截图和录像均不增加。页面刷新和等待计时不能改变结果。

### 2. 摄像头断开

先持续取帧，再断开或受控使读取失败。健康状态应离线；帧号停止，旧帧不重复推理；不生成 movement。重连建立新的 `source_session_id` 和基线，断开/重连不能作为位移。

### 3. 静止物品五分钟

真实物品或标签在一个区域静止至少五分钟，允许手从旁边经过但不触碰物品。`item_current_state.last_seen_at` 应继续更新，movement 计数、截图和录像保持不变。

### 4. 同区轻微调整

在同一区域移动小于 `same_zone_move_distance`。当前坐标和最后看到时间可更新；不得新增历史事件。

### 5. 真实跨区移动

物品在桌面稳定后移到沙发并重新稳定。预期恰好一条 `event_type=movement`，`from_zone=桌面`、`to_zone=沙发`，当前状态更新；不存在独立 picked_up/moved/placed 历史行。

必须检查截图可解码、短片能读取帧、文件哈希匹配、事件时间落在短片覆盖范围，且记录与媒体的 `source_session_id` 一致；还要重新计算 `event_media.metadata.write_binding` 的 digest，确认模式、来源、会话、帧/时间范围及输入/输出哈希一致。普通 v1 movement 使用一张前后拼图，不能重复计为两张媒体；P0 v2 则必须有独立的 before、after 和 clip，视频覆盖移动前后各至少 5 秒，不能用同一路径替代两张独立关键帧。

### 6. 严重遮挡

移动中完全遮挡，且没有在新位置重新检测到。不得生成 confirmed movement；状态只允许“被遮挡”“丢失”或“最后看到”，不能猜测沙发等具体终点。

### 7. 页面刷新

记录刷新前事件数，连续刷新页面，事件数和媒体数必须完全不变。GET 请求、轮询和 WebSocket 订阅不能执行 seed 或事件写入。

### 8. 后端重启

记录事件和当前状态，正常停止并重启 REAL。不得 seed、重新消费旧 movement 或因摄像头重开生成事件；当前状态可以恢复。摄像头重新开始后需重新稳定。

### 9. 演示视频循环

只在 DEMO 执行。事件必须是 `DEMO/video_file/true`，数据库和媒体与 REAL 分开；同一轮 movement 去重，新一轮使用新 demo source session；页面始终显示演示横幅。

### 10. 虚拟 ESP32

只在 DEMO/TEST 执行。设备必须显示“虚拟设备”，来源为 `virtual_esp32`，不得进入 REAL 数据库，也不得出现“物理板刷写成功”。

## 结果检查

每个场景至少记录：

- 启动命令和退出码；
- 当前运行模式、数据库和媒体根；
- 来源类型、摄像头 ID 和来源会话；
- 测试前后当前状态/事件/媒体数量；
- 关键帧编号和时间；
- 截图、短片路径与 SHA-256；
- 被拒绝候选的原因；
- 是否真实物理摄像头；
- 人工步骤和无法自动验证之处。

本阶段机器可读分类结果见 [final-test-results.json](../audit/p0-real/final-test-results.json)，第三次正式长测结果见 [runtime-stability.json](../audit/p0-real/runtime-stability.json)。首次失败原始结果保留在 [runtime-stability-first-attempt.json](../audit/p0-real/runtime-stability-first-attempt.json)，第二次正常退出失败原件保留在 [runtime-stability-second-attempt.json](../audit/p0-real/runtime-stability-second-attempt.json)，汇总结论见 [审计报告](AUDIT_REPORT.md)。根层 `audit/test-results.json` 和 `audit/runtime-stability.json` 是初次审计历史产物。未实际执行的场景必须写 `NOT_RUN`，不能用测试代码存在替代。

## 本轮执行状态

| 场景 | 当前状态 |
|---|---|
| 无视频源 | 核对本阶段总表的 REAL FastAPI/SQLite 浏览器 E2E 分类；不得用摄像头已启动后的长跑替代无源测试 |
| 摄像头断开 | 软件 session/epoch 回归与 P0 实物场景分开；物理场景 `NOT_RUN` |
| 静止物品五分钟 | 模拟时钟软件回归不等于物理五分钟；P0 实物场景 `NOT_RUN` |
| 同区轻微调整 | 软件几何/状态机回归与实物验收分开；P0 实物场景 `NOT_RUN` |
| 真实跨区移动 | P0 实物十次跨区移动 `NOT_RUN`，合成标签和文件视频不得计入 |
| 严重遮挡 | 软件状态机回归与实物验收分开；P0 实物场景 `NOT_RUN` |
| 页面刷新 | 核对本阶段总表的真实后端浏览器 E2E；刷新不得新增事件或媒体 |
| 后端重启 | 核对本阶段总表的进程重启集成测试；不得重新 seed 或恢复旧候选 |
| 演示视频循环 | 核对本阶段 DEMO 文件视频分类及隔离库/媒体证据，不计为物理识别 |
| 虚拟 ESP32 | 核对本阶段虚拟协议分类；不计为物理设备或刷写证据 |

初次审计的 1,800.047 秒及其通过结论仅是历史记录。本阶段首次长跑实际完成 **1,800.109 秒，30 门中 2 门失败**：出现 1 条未确认 `last_seen` 当前状态，movement 和事件媒体仍为 0，`zero_unobserved_events` 与 `honest_empty_search` 不通过。视觉已到 `LOST` 而持久状态仍为 `last_seen`；原触发帧未保存，无法视觉复核具体误识别纹理。失败原始产物不得被后续成功覆盖。

普通观察写入门和丢失状态计时修补后，第二次长跑实际完成 **1,800.125 秒，29/30 门通过**；movement、当前状态、轨迹和事件媒体均为 0，未触发异常帧留存。`zero_unobserved_events`、`honest_empty_search` 和 `camera_released` 通过，但唯一失败门 `backend_stopped_cleanly` 为 false：`graceful=false`、退出码 1，后端日志止于 `Shutting down`。该失败原件保留，不能用成功释放摄像头替代正常退出。

退出修复后的第三次正式长跑已完成：**soak 1,800.094 秒、总观察 1,808.812 秒、30/30 门通过、`errors=[]`**。movement、当前状态、轨迹和事件媒体均为 0；后端 `graceful=true`、退出码 0，`camera_released=true`，隔离临时数据已清理。本次无事件长跑未覆盖人工纠正等未执行操作，不能把稳定性通过升格为物理手机移动成功。

最新判定及各分类实际数量以本阶段 `audit/p0-real/final-test-results.json`、`audit/p0-real/runtime-stability.json` 和保留的 `audit/p0-real/runtime-stability-first-attempt.json`、`audit/p0-real/runtime-stability-second-attempt.json` 为准，不搬用旧测试数字。P0 实物套件全部 14 个场景仍为 `NOT_RUN`。

## 测试等级分开报告

本阶段最终 Python 为 **345 passed，0 失败/错误/跳过**（JUnit 时间 235.448 秒，源文件 `data/verification/pytest-observation-final.xml`）；其中包括 Mock 单元与实际后端/数据库集成，不全部算作真实视频验收。真实 FastAPI＋独立 REAL SQLite 浏览器 E2E 为 **2 passed / 6.975 秒**；DEMO 文件视频 E2E 为 **1 passed / 34.196 秒**。后两类均无失败、跳过或 flaky，原始 JSON 及证据范围在本阶段总表分别列出；物理手机 14 轮仍不计入。

- Mock 单元/界面契约测试；
- 真实 FastAPI + 独立 TEST SQLite 集成测试；
- 真实文件视频 DEMO 测试；
- 物理电脑摄像头取帧测试；
- 物理摄像头物品 movement 测试；
- 虚拟设备协议测试；
- 固件编译测试；
- 物理 ESP32 真机刷写和取流测试。

各类数量、退出码和失败项分别核对本阶段总表，不把 Mock、软件集成、文件视频或编译通过相加为硬件验收。物理物品移动结论须有当次真实摄像头移动证据，ESP32 真机结论须有当次物理板刷写与取流证据；虚拟协议和固件编译都不能替代。

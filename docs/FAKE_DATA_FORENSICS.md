# 假数据与模拟入口取证

## 结论

审计前项目确实存在会被普通运行页面看到的模拟事件。最严重的问题不是某个 React 数组，而是 REAL、DEMO 和虚拟设备共用 `data/database/object_memory.sqlite3`，旧查询又把任何 `placed` 当成确认位置。结果是测试回放和虚拟 ESP32 产生的“手机从桌面到沙发”可以进入默认搜索、首页和事件页。

当前实现已经把模式、数据库、媒体、来源字段、查询过滤和 Mock 测试入口分开。正常 `Start.bat` 强制 REAL；没有真实证据时不返回固定位置。DEMO 与测试能力仍保留，但必须显示模拟来源。

本文件说明入口、来源和历史清理。初次清理数量由 `audit/cleanup-result.json` 记录，不计入本阶段新增删除；本阶段清理见 `audit/p0-real/cleanup-result.json`。当前分类测试见 [final-test-results.json](../audit/p0-real/final-test-results.json)，汇总结论见 [审计报告](AUDIT_REPORT.md)。第二次长跑的 29/30、正常退出失败原件保留在 [runtime-stability-second-attempt.json](../audit/p0-real/runtime-stability-second-attempt.json)；退出修复后第三次正式长测已完成 30/30，结果见 [runtime-stability.json](../audit/p0-real/runtime-stability.json)。这是本机取流稳定性通过，不是实物 14 轮验收通过。根层 `audit/test-results.json` 仅是初次审计历史产物。

## 问题分级

| 级别 | 审计发现 | 当前处置 |
|---|---|---|
| P0 | REAL/DEMO/虚拟设备曾共用一个 SQLite，测试事件可被默认搜索当成确认位置 | 三模式独立 DB/媒体，REAL 双重来源过滤，旧事件不迁移 |
| P0 | 旧状态机把 seen/picked_up/moved/placed 分开写库，未要求完整证据会话 | 改为一条完整 movement + `item_current_state`，写库前执行证据门 |
| P0 | 视频读帧成功、Mock API 和虚拟设备状态曾被页面使用“真实”类措辞 | 持续模式横幅、显式来源 badge、设备证据分项、Mock 测试单独命令 |
| P0 | 浏览器上传帧或 ESP32 网络 claim 可能被名称/在线状态包装成物理来源 | `browser_camera` 仅保留“最后看到”；网络 claim 标成 `esp32_unverified` 且不绑定摄像头，物理身份只由 REAL 本机 USB + 匹配 `OMREADY` 建立 |
| P1 | 循环视频和虚拟设备反复生成相同媒体，旧库 1056 个媒体中 1008 个内容重复 | source session、fingerprint、幂等唯一键和 MINIMAL retention |
| P1 | 媒体哈希正确曾被误当作来源真实性 | provenance 文档明确“完整性不等于来源”；REAL 还核对模式、会话和摄像头 |
| P2 | 生产 UI 保留手机/桌面/沙发的输入建议，容易被静态搜索误判为硬编码答案 | 保留为无副作用提示；E2E 明确断言空 REAL 不出现这些结果 |

## 检查范围和方法

以审计前 checkpoint `f9e1205`、当前工作树、`audit/before-audit.json` 和 SQLite 只读查询交叉核对。全项目搜索了以下关键词及大小写、连字符和下划线变体：

```text
mock fake faker fixture sample seed demo simulate simulation virtual fallback
hardcoded placeholder generate_demo demo_seed mockServiceWorker msw json-server
setTimeout setInterval Math.random random sofa phone placed picked_up
last_location event test-video sample-video
```

搜索排除了 `node_modules`、`.venv*`、`__pycache__`、`.pytest_cache`、前端 `dist`、PlatformIO 缓存、构建产物和 `data` 大媒体；数据库与媒体则另行按表、来源和 SHA-256 审计。通用词 `event` 的命中量很大，按“写入入口、查询入口、媒体入口、测试入口、显示入口”逐类审查，而不是把每个类型声明重复列为独立假数据源。

未找到 Faker、`mockServiceWorker`、运行时 MSW worker 或 json-server 的生产使用入口；文档中的否定说明本身会命中这些关键词，不能把全文命中数误报为运行时依赖。`apps/web/package-lock.json` 中的 `msw` 只是 Vitest mocker 的可选 peer 声明，`package.json` 没有安装 MSW，生产代码没有 import 或 worker 注册。

### 2026-09-05 增量搜索记录

本阶段重新搜索整个项目自编文本，包含前后端、视觉、固件、脚本、测试、演示输入与文档；使用 `--hidden --no-ignore`，再显式排除依赖、缓存、构建结果、历史审计输出、运行数据库/媒体和所有 `.env*`。依赖锁文件仍包含在内；`data` 没有被当成源码 grep，而是另做 SQLite 只读核验。二进制演示图片/视频由生成入口和实际文件视频验收解释，不把无法 grep 像素等同于“没有模拟”。

完整正则、参数数组、排除理由、扫描文件数、总匹配行数、每关键词/类别/文件的当前计数保存在 [fake-data-search.json](../audit/p0-real/fake-data-search.json)。计数只表示关键词命中，不表示假事件数量；同一行可同时命中多个词，分词计数不能求和。结果不保存源码行、摄像头凭据或任何环境密钥值。

可按记录中的参数重跑同一搜索：

```powershell
$scan = Get-Content -LiteralPath '.\audit\p0-real\fake-data-search.json' -Raw | ConvertFrom-Json
$scanArgs = @($scan.search_argv | Select-Object -Skip 1)
& rg @scanArgs
```

该次增量搜索时另以 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 读取规范 `data/database/objectmemory.sqlite`：`quick_check=ok`，移动事件、当前状态、事件媒体均为 0，`real-media` 文件为 0。当时旧运行库尚无新 P0 套件/轮次表；这是上述 JSON 内检查时间对应的历史快照，不是当前迁移状态或验收完成证明。当前数据库状态和 P0 `NOT_RUN` 结论以本阶段总表与审计报告为准。

## 审计前旧混合数据库

只读检查 `data/database/object_memory.sqlite3` 得到：

| 项目 | 审计前事实 |
|---|---:|
| 事件 | 528 |
| `seen` | 9 |
| `picked_up` | 131 |
| `moved` | 259 |
| `placed` | 129 |
| “我的手机”事件 | 528 |
| 虚拟 ESP32 摄像头事件 | 521 |
| 演示视频摄像头事件 | 7 |
| `test_replay` | 525 |
| `aruco_motion` | 3；仍来自演示视频摄像头，不是物理摄像头 |
| 媒体注册行 | 1056 |
| 实际存在且登记哈希匹配的媒体 | 1056 |
| 不同截图内容 | 19 |
| 不同短片内容 | 29 |
| 内容重复媒体 | 1008 |

三个旧物品都是固定演示 ID：`demo-phone`、`demo-keys`、`demo-wallet`。旧摄像头包括演示视频、一个电脑摄像头配置和 `config.simulated=true` 的虚拟 ESP32，但 528 个事件没有一个来自电脑摄像头。

因此这些旧事件不能分类为 `real_verified`。媒体存在、可读或 SHA-256 一致只证明文件没有错位，不证明像素来自真实家庭摄像头。1008 个内容重复媒体也证实旧状态机会把相同回放反复落盘。

审计前磁盘基线为：项目总计 691,511,984 字节/20,091 文件；排除依赖和 data 的源码 21,589,102 字节/184 文件；data 79,629,119 字节/1,537 文件；图片 33,593,665 字节/697 文件；视频 26,937,490 字节/669 文件；日志 60,143 字节/13 文件；固件产物 17,181,720 字节/4 文件。上述是存在性统计，不代表真实性。

实际清理删除 528 条事件、529 张截图（含 1 张孤儿图）、528 段录像和 2 条轨迹，文件删除失败和其他失败均为 0。498 条重复事件因全部属于 DEMO/virtual 而直接删除，合并数为 0；`demo-phone` 剩余 0 条，unknown 为 0。阶段性 `data` tree 从清理执行前 80,648,509 字节降到 33,798,413 字节；随后另清理 431 个自动化临时文件/18,151,906 字节、151,552 字节的已清空 legacy DB，以及 196 个可重建缓存、过期验收产物和 SQLite 临时边车/5,661,300 字节。最终同口径快照为项目 647,364,741 字节/18,534 文件，`data` 14,188,960 字节/32 文件；这些嵌套口径不能自行重复相减。

上述 528 条事件及截图、录像、缓存删除量均为初次审计的历史清理事实，来源是根层 `audit/cleanup-result.json` 与当时快照；不得计入本阶段 `audit/p0-real/cleanup-result.json`，也不能用旧磁盘快照描述当前大小。

## 模拟与固定数据入口清单

表中的“REAL”指当前正常生产模式；“DB/搜索/媒体”说明该入口在允许的模式下是否会产生相应影响。

| 入口 | 文件与代码位置 | 触发条件 | REAL | DB / 搜索 / 媒体影响 | 当前状态与验证 |
|---|---|---|---|---|---|
| 审计前共享数据库 | checkpoint `apps/api/app/main.py` 的 `Runtime.__init__` | 任意模式启动 | 曾会 | 所有摄像头、测试视频、虚拟设备和真实配置共用 `object_memory.sqlite3`；默认查询可见 | 已改为 `runtime_mode.py` 的三库三媒体布局；旧事件不迁移到规范 REAL |
| 审计前逐阶段事件 | checkpoint `services/vision/events/state_machine.py`、`Runtime.event` | 每次看到、拿起、过区、放稳 | 曾会 | 每阶段写 `events`，每行可存截图/片段；循环回放造成膨胀 | 当前只发一条完整 `movement`；普通观察更新 `item_current_state` |
| 审计前搜索信任 `placed` | checkpoint `apps/api/app/search.py:location_result` | 查询匹配到任意旧 placed/manual | 曾会 | 把测试库中的沙发位置作为“最后确认” | 当前 REAL 查询按 mode/source/simulated/evidence 过滤；无证据返回诚实空答案 |
| demo seed API | `apps/api/app/main.py:1855 seed_demo` | DEMO 中显式 POST 或无硬件启动脚本 | 拒绝 409 | 只 upsert DEMO 物品、视频摄像头和区域；本身不创建 movement；后续回放事件仅进 DEMO 搜索/媒体 | 后端模式门；响应为 `DEMO/demo_seed/true` |
| 合成素材生成器 | `scripts/generate-demo-assets.py:90 generate_demo_video`、`:184 write_seed_manifest` | bootstrap 或手工运行 | 只生成文件 | 写 `demo/markers`、`demo/sample-videos`、`demo/seed-data`；不写数据库、不产生搜索答案或事件媒体 | 素材画面带测试标记；bootstrap 后仍初始化空 REAL |
| bootstrap 调用生成器 | `scripts/bootstrap.ps1`、`scripts/init-project.py` | 首次安装 | 允许生成文件 | 创建演示资产但 `create_app(runtime_mode="REAL")`；不调用 demo seed | 已阻断“安装即写演示事件” |
| 视频文件摄像头 | `services/vision/camera_sources/opencv_stream.py:378 VideoFileSource`、`services/vision/engine.py:105` | DEMO/TEST 配置并启动 video | 配置/启动被拒绝 | 可经真实检测链写 DEMO/TEST movement、截图和短片；`video_file/true` | 回绕标记 discontinuity、新 source session；同轮 fingerprint 和 DB 唯一键去重 |
| 视频默认路径 | `apps/web/src/pages/CamerasPage.tsx:231`、`services/vision/engine.py:106` | DEMO 选择“视频文件回放” | 页面不显示该选项，后端也拒绝 | 只帮助选择项目合成视频；不会在页面刷新时启动 | 保留为明确 DEMO 便利项，不是 REAL fallback |
| 虚拟 ESP32 | `tools/virtual_esp32cam/device.py`、`apps/api/app/firmware.py:799 start_virtual_device`、`scripts/simulate-device.py` 兼容入口 | DEMO/TEST 显式启动或无硬件脚本 | 后端拒绝 | 走真实 claim/token/heartbeat/JPEG/MJPEG；设备、摄像头、事件和媒体都只进 DEMO/TEST | 强制 `virtual_esp32/true` 和“虚拟设备 · 非真实硬件” |
| 浏览器帧上传 | `apps/web/src/components/BrowserCamera.tsx`、`apps/api/app/main.py:1560 browser_camera_ingest` | 用户授权页面后经 WebSocket 上传 JPEG | 可建立观察 | 服务端能校验会话、Origin、消息格式和帧连续性，但不能证明 JPEG 来自物理 `getUserMedia` | REAL 强制 `browser_camera` 来源未验证，只允许“最后看到”；confirmed/placed 不进入真实位置答案 |
| RTSP/ONVIF/MJPEG 配置流 | `apps/api/app/main.py` 摄像头配置与网络源校验、`services/vision/camera_sources` | 管理员显式配置私有网络流 | 只可观察 | 后台可收帧，但协议、URL、名称和在线状态都不能证明硬件厂商或物理身份 | REAL 强制只更新“最后看到”，不得写确认 movement；页面标成“网络视频流（物理来源未验证）” |
| ESP32 网络 claim | `apps/api/app/firmware.py` 的 enrollment/claim/heartbeat | 设备持一次性配对码从局域网领取 token | 非模拟 claim 只建待验证记录 | REAL 返回 `hardware_type=unverified`、`source_type=esp32_unverified`；不绑定摄像头，不能生成 REAL 确认事件。DEMO/TEST 模拟 claim 只进入隔离虚拟链路 | 只有 REAL 本机合格 USB 串口收到并核对匹配 `OMREADY` 后才升级 `physical/esp32_real`；DEMO/TEST 禁止物理串口操作 |
| 无硬件一键演示 | `scripts/start-demo-no-hardware.ps1`、`scripts/start_no_hardware_demo.py` | 用户运行专用脚本 | 另起 DEMO 进程 | seed DEMO，启动虚拟设备并等待测试视频事件 | 与 `Start.bat` 分离；页面持久演示横幅 |
| DEMO/虚拟验收脚本 | `scripts/verify-running-demo.py`、`scripts/verify-virtual-device.py` | 手工/测试命令 | 脚本指定 DEMO | 会在隔离/DEMO 库创建配置并等待沙发事件；可生成 DEMO 媒体 | 属于测试证据，不是物理摄像头或真机证据 |
| 授权屏幕技术验收 | `scripts/verify-authorized-screen.py` | 脚本打开合成视频窗口并程序化选区 | 脚本指定 DEMO | 可能创建 DEMO 屏幕摄像头、movement 和媒体 | 必须描述为“合成窗口技术验收”，不是厂商监控或人工框选实测 |
| Mock 浏览器 E2E | `apps/web/tests/e2e/app.mock.spec.ts:10 mockApi` | `npm run test:e2e:mock-contract` | 不连接 REAL 服务 | `page.route` 固定手机、桌面、沙发、08:32、95%；预览用微型 JPEG，事件媒体返回空字节；不写 SQLite | 已移出默认配置，单独 `playwright.mock.config.ts`，测试名写明 ui-mock |
| 前端单元 Mock | `apps/web/tests/*.test.tsx`、`tests/api.test.ts`、`tests/setup.ts` | Vitest | 不进入生产 | Mock fetch、媒体权限和组件输入；不写真实 DB/媒体 | 保留为单元/组件测试，报告必须单列 |
| 后端/视觉 fixture | `apps/api/tests`、`services/vision/tests` | pytest 的 TEST 或隔离临时 REAL/DEMO 语义库 | 不进入规范运行 REAL 库 | fake capture、monkeypatch、合成帧或文件视频；验证状态机/安全/媒体。临时库中标为 REAL 的测试输入也不能当作物理来源证据 | 保留；测试目录与运行数据独立，报告单列输入是否合成 |
| 静态查询建议 | `apps/web/src/pages/DashboardPage.tsx:47`、`apps/web/src/pages/SearchPage.tsx:43` | 用户点击“我的手机在哪里”等按钮 | 可见 | 只填查询文本并调用实际 `/api/search`；没有静态结果数组，不写事件/媒体 | 合法 UI 提示；REAL 空结果仍为空 |
| 静态区域/表单示例 | `ZoneEditorPage.tsx:11`、`ItemsPage.tsx` | 用户新建配置 | 可见 | “桌面/沙发/手机”等只是输入建议；只有用户提交才写配置，不创建事件 | 不是位置答案，也不带时间/可信度 |
| `setInterval` | `AppShell.tsx:31`、`useApi.ts`、`BrowserCamera.tsx`、`DevicesPage.tsx` | 页面挂载/设备流程 | 可用 | 轮询 runtime/health、浏览器推帧、设备 job/倒计时；不调用事件创建接口 | 审查未发现定时生成事件；组件卸载清理 timer |
| `setTimeout` | `Toast.tsx`、`CompanionPage.tsx`、摄像头诊断与测试 | 提示消失、WS 重连、音频/测试等待 | 可用 | 不构造位置或事件；测试 timeout 只控制失败边界 | 不是假事件入口 |
| `Math.random` | `Toast.tsx:13`、`CameraDiagnosticsPage.tsx:57`、摄像头技术验收脚本 | toast ID 或请求 cache bust | 可用 | 不用于 confidence、位置、时间、event ID 或 DB 内容 | 事件和安全 token 用 UUID/secrets |
| 算法 fallback | `services/vision/engine.py:224 _active_detection_mode`、`apps/web/src/components/ModeNotice.tsx` | 实验模型不可用 | 可用 | 降级到 ArUco；不生成固定检测或事件 | 页面显示“回退稳定标签模式”，不能称无标签 AI |
| 固件 fallback pairing | `firmware/esp32cam/src/main.cpp:233` 条件、`:235` 日志 | Wi-Fi 失败超过约 30 秒 | 物理设备行为 | 开启配网 AP，不生成物品事件 | 是安全限制，不是假位置；见硬件文档 |
| 搜索文本模板 | `apps/api/app/search.py:26 location_result` | 对实际 state/event 格式化 | 可用 | 房间/区域来自记录；没有固定桌面、沙发、16:32 或 91% | 无证据时 REAL 固定的是“暂无真实记录”，不是位置 |
| 旧类型兼容 | `apps/web/src/types.ts`、`EventsPage.tsx` | 查看旧审计数据 | 可见 | 仍可识别 placed/picked_up/moved/seen 名称，但规范 DB 只写 movement/manual | 页面标注旧数据；REAL 来源过滤阻止旧脏记录成为答案 |

### 本阶段新增 P0 与接入入口

| 入口与位置 | 触发与实际写入 | 是否可制造自动确认事件 | 当前复核结果 |
|---|---|---|---|
| `apps/api/app/acceptance.py:288 marker_png`、`apps/api/app/main.py:1125 acceptance_marker_png` | GET 生成带白边的真实 ArUco PNG 字节并直接响应；不写物品、事件、截图或录像表 | 否；它是要展示给摄像头的标记，不是已经识别到物品的证据 | `generateImageMarker` 在此是合法显示素材，不能据此称 P0 已通过 |
| `apps/api/app/acceptance.py:260 bind_marker`、`apps/api/app/main.py:1110 bind_acceptance_marker`、`apps/web/src/pages/ValidationMarkerPage.tsx:139` | 用户点击绑定，更新既有物品的 `aruco_id`；有运行摄像头时重新建立源状态 | 否；不插入 movement，也不提交完成结果 | 活跃验收不能重绑；标记显示页不采集手机摄像头。轮询只读后端实际识别状态 |
| `apps/api/app/acceptance.py:325 create_suite`、`:502 create`；`apps/api/app/db.py:108-114` | 用户创建固定 14 场景套件和下一个轮次；写的是工作流表，不是物品事件表 | 否；额外的 `overall_passed`、`outcome` 等客户端结果字段被拒绝 | 物品、相机配置指纹、区域及合同由服务端冻结；唯一约束阻止重复场景，失败/取消仍占原槽位 |
| `apps/api/app/main.py:1159 start_acceptance_run`、`:1180 disconnect_reconnect_acceptance_run` | 只启动已建立的服务端轮次或执行受控断开；接口拒绝附带结果 | 否；断流不转为移动，必须重新建立连续基线 | 没有 `/complete`、客户端 PASSED 或原始事件提交端点；取消只有失败/取消语义 |
| `apps/web/src/pages/RealMovementAcceptancePage.tsx:247`、`apps/web/src/lib/api.ts:72 acceptanceApi` | 有 `suite` 时只恢复该套件；无套件时可用 `item/camera/zoneA/zoneB` 准备选择，挂载/刷新只 GET；明确按钮才创建或启动轮次 | 否；客户端没有自动事件结果上传 | 静态 `ACCEPTANCE_SCENARIOS` 是操作合同镜像，不是静态事件数组；最终通过由服务端汇总，不接受 `runs/current` 挑选历史成功样本 |
| `services/vision/acceptance.py`、`services/vision/engine.py:779 _enqueue_acceptance_event_locked` | 同一来源的检测帧完成起点稳定、连续离区/中途、终点重新稳定，再收满前后视频窗口 | 可生成候选；只有 EventService 复核并原子提交后才确认 | 遮挡会清空连续稳定窗口；断流、队列/保存失败不会变成成功。OpenCV 类型仍不证明物理镜头身份 |
| `apps/api/app/event_service.py:168 _validate_physical_acceptance`、`:392 _validate_evidence`；`apps/api/app/main.py:427 Runtime.event` | 内存视觉回调提交实际帧；核对套件、会话、几何轨迹、解码标签位置、逐帧内容和媒体哈希 | 唯一自动写入门；客户端无法直接调用其 Python 回调 | 独立 before/after/clip 与状态、事件、轮次同事务绑定；单纯哈希正确或全画面平均误差小都不够 |
| `services/vision/tests/test_real_acceptance.py:163 feed_engine`、`apps/api/tests/test_physical_acceptance.py:176 acceptance_clip_frames` | 测试生成可真实解码的合成标签帧，注入隔离引擎/SQLite 以验证证据门 | 只可在测试运行里产生合成记录；无生产导入路径 | 这些“可解码像素”仍是合成输入，不能计入物理手机移动次数；生产唯一标签图片生成接口只负责显示 |
| `apps/api/app/main.py:1749 correct_event` | 管理员明确纠正既有有效事件的位置，产生 `manual_correction` 并保留引用 | 是人工操作，不是自动视觉确认 | 不应把“没有客户端自动事件提交入口”写成“所有客户端操作都不写事件”；纠正保持人工标识与幂等 |
| `scripts/serve.py:31 listener_metadata`、`apps/api/app/firmware.py:362 listener_status`、`:401 _verify_backend_listener`；`apps/web/src/pages/DevicesPage.tsx` | 当前进程真实 LISTEN 元数据决定安装/配网是否开放；LAN URL 只作候选 | 不产生物品事件，也不证明板卡可达/刷写 | 无 LAN 监听证明即在入队与硬件步骤前拒绝；纯编译不被包装成设备成功 |
| `apps/api/app/main.py:1187 acceptance_preflight`、`apps/web/src/pages/RealMovementAcceptancePage.tsx:218 companionMarkerUrl`、`:492`、`:805 CompanionAccess` | preflight 只从当前进程已验证且已启用的实际 listener `lan_urls` 生成手机 Marker 候选地址；页面附加 item/camera/可选 run | 否；无 suite/run 也可显示 Marker，不会为链接或二维码创建验收记录 | 不接受前端任意 URL 或 Host 伪造候选；无 LAN 时后端不提供 LAN 地址。没有二维码时明确使用链接，仍需同一局域网和访问码；监听真实不代表手机已可达 |

本阶段独立执行了 `test_acceptance_http_control_plane_has_no_client_completion_endpoint` 与 `test_real_rejects_demo_sources_and_has_honest_empty_search`，**2 passed**。二者使用真实 FastAPI 处理器和隔离 SQLite，不 Mock 核心业务 API：确认空 REAL 拒绝 seed、video、virtual 并保持零事件，套件拒绝客户端通过结果。它们不是浏览器 E2E、物理摄像头或真机验收；完整测试总数由当前 [审计报告](AUDIT_REPORT.md) 单独记录。

### 本阶段长跑暴露的普通观察入口

普通观察另经 `services/vision/engine.py:1026 _emit_track`、`:1068 _emit_missing_track` → `apps/api/app/main.py:501 Runtime.track` → 当前状态及搜索；“不能直接提交确认 movement”不等于“普通观察不会写当前状态”。本阶段首次 1,800.109 秒长跑有 2 门失败：出现注册标记 49 的 1 条未确认 `last_seen`，视觉状态已为 `LOST` 而持久状态仍为 `last_seen`；movement 与事件媒体均为 0。此异常不是已确认位置变化，也没有证据把它归为 seed 或 Mock。

原始触发帧未保存，因此不能确定具体误识别画面；后续独立 30 秒、885 帧未识别到标签以及一张当前画面，都不能替代原帧。原始失败保存在 [runtime-stability-first-attempt.json](../audit/p0-real/runtime-stability-first-attempt.json)，不因后续复测覆盖。

普通观察写入门和丢失状态计时修补后的 [第二次长跑原件](../audit/p0-real/runtime-stability-second-attempt.json) 实际完成 **1,800.125 秒，29/30 门通过**：movement、当前状态、轨迹和事件媒体均为 0，未触发异常帧留存。唯一失败门是 `backend_stopped_cleanly`，`graceful=false`、退出码 1、日志止于 `Shutting down`；`camera_released` 通过不能替代正常退出。退出专项已在 `scripts/serve.py:18 run_server` 实现 Windows 单次 Selector 事件循环修复，不修改标准库或抑制异常。

[第三次正式长测](../audit/p0-real/runtime-stability.json) 已完成 **soak 1,800.094 秒、总观察 1,808.812 秒、30/30 门通过、`errors=[]`**；movement、当前状态、轨迹和事件媒体均为 0，后端正常退出 `graceful=true`、退出码 0，摄像头释放通过，隔离临时数据已清理。前两轮失败事实及首轮原帧未知仍保留；本次零事件长跑不覆盖人工纠正等未执行操作，更不证明实物手机移动成功。

新增 `scripts/runtime-stability.py` 异常诊断只在异常触发后有界保存至多 3 张实际 API JPEG 及诊断 JSON，不定期保存每帧，不创建事件；无法原子对应触发帧时明确记录 `not_same_frame`，不伪造帧号、会话或时间对应关系。P0 实物套件 14 个场景仍为 `NOT_RUN`。

## 审计前哪些页面可能显示模拟数据

以下结论来自审计前 checkpoint，而不是凭旧 README 推测：

1. `DashboardPage.tsx`：所有模式的空事件页都显示“准备演示闭环”，描述为“第一次真实识别”；按钮可调用共享库的 demo seed。只有首页局部无硬件卡片有模拟提示，其他页面没有全局模式横幅。
2. `SettingsPage.tsx`：演示保障 section 在普通模式也显示，可直接调用 seed；清理“全部事件”与普通清理边界过近。
3. `CamerasPage.tsx`：视频文件选项在普通向导中可选，默认填入合成视频；视频读帧成功也统一提示“已读取到真实画面”，混淆了真实摄像头与文件像素。
4. 搜索页/`SearchResultCard`：使用共享 DB，旧 `placed` 即可成为确认答案；来源主要靠 `evidence_type=test_replay` 的文本，缺少 mode/source/simulated 的强制判断。
5. 事件页和物品页：共享事件列表包含虚拟/视频记录，来源未知的旧记录可显示为普通确认放下。
6. 设备中心：虚拟协议与固件 job 虽有部分说明，但编译任务使用“任务真实完成”等模糊语言，且没有把编译、物理板、刷写、配网和真机视频分成独立证据项。
7. 默认 Playwright：`playwright.config.ts` 的 `testDir` 同时包含 `app.mock.spec.ts` 和所谓 real spec；后者实际使用文件视频和 demo seed，却以“真实隔离后端”命名，容易把“真实 API”误报为“真实摄像头”。

当前修复是全局模式横幅、显式来源 badge、REAL 后端阻断、三套 Playwright config 分离、设备证据分项，以及搜索 fail closed。浏览器帧和 RTSP/ONVIF/MJPEG 都只显示“最后看到 / 来源未验证”，也不能写 REAL 确认 movement；网络 ESP32 claim 只保留未验证记录，直到 REAL 本机 USB 串口返回匹配 `OMREADY`。UI 隐藏不是唯一防线；后端仍独立拒绝模拟来源和不合格物理操作。

## 二十项重点核对

| 核对项 | 当前结果 |
|---|---|
| 1. API 失败返回固定事件 | 未发现。`useApi` 暴露错误，搜索开始/失败会清除旧结果；没有 fallback 静态事件。 |
| 2. 写死手机/桌面/沙发/时间/可信度/媒体 | 生产中只有输入建议和 DEMO 配置；08:32、95%、固定媒体只在 Mock 测试。`16:32` 只出现在 REAL E2E 的负向断言。 |
| 3. MSW 或其他拦截真实请求 | 生产无 MSW/json-server。只有独立 Mock Playwright 用 `page.route`。 |
| 4. 生产构建启用 Mock worker | 无 worker 文件、注册或依赖；生产构建不启用。 |
| 5. 后端启动自动 seed | 不会。seed 只能显式调用且只允许 DEMO。 |
| 6. 正常 Start 自动演示事件 | 不会。`scripts/start.ps1` 强制 REAL；没有 seed 或虚拟设备调用。 |
| 7. 空库查询返回样例 | 不会。REAL 返回“暂时没有真实摄像头产生的位置记录。” |
| 8. 视觉服务未启动但定时生成事件 | 未发现。前端 timers 仅轮询/推帧/倒计时；历史写入只走 `EventService`. |
| 9. 摄像头断开仍产生事件 | 状态机在帧 gap、session/epoch 变化时重置候选；受控测试通过，物理拔线场景仍未执行。 |
| 10. 虚拟或未验证 ESP32 写 REAL | 虚拟设备只在 DEMO/TEST 启动；非模拟网络 claim 在 REAL 也只建 `esp32_unverified` 待验证记录，不绑定摄像头或写确认事件。只有 REAL 本机合格 USB + 匹配 `OMREADY` 才成为 `esp32_real`。DEMO/TEST 禁止 flash/provision/install 和页面上的物理设备申领，模拟配对仍只留在隔离库。 |
| 11. 视频循环无限复制 | 回绕建立新 stream epoch/source session；同轮有 fingerprint 与幂等唯一约束。每个明确新 DEMO session仍可生成一轮，可由 retention 限制。 |
| 12. 页面刷新重新建 demo | 不会。页面挂载是 GET/WS；seed 只绑定 DEMO 的明确按钮。 |
| 13. React 静态事件数组 | 生产 `src` 未发现。静态 event 在 `tests` 的 Mock fixture。 |
| 14. SQLite 残留 seed/test | 旧混合库清理事实见历史小节；增量搜索时的只读零记录是带时间的快照。当前规范 REAL 与长跑隔离库须分开，以本阶段总表为准；P0 实物 14 场景仍为 `NOT_RUN`。 |
| 15. 截图/录像是预置素材 | 旧 1056 个媒体大量由同一回放重复生成；当前 DEMO 仍从测试视频生成并明确标记。REAL 只有证据门通过才保存。 |
| 16. 浏览器测试只验证 Mock | 审计前默认套件混入 Mock 且 real spec 实为文件视频。当前 REAL 真实 FastAPI/SQLite E2E 2/2 通过，DEMO 文件视频 E2E 1/1 通过，Mock 使用独立命令且不计入真实链路。 |
| 17. “我的手机在哪里”无数据固定答案 | 当前没有。固定问题只是 query suggestion；答案从可信事件/当前状态生成。 |
| 18. 虚拟或网络申领设备显示成物理硬件 | 虚拟设备显式 `hardware_type=virtual`、`virtual_esp32/true`；仅网络申领的非模拟设备显式 `unverified/esp32_unverified` 且无摄像头链接。两者都不会显示成物理设备。 |
| 19. ArUco 描述成无标签 AI | 当前标签模式与实验模式分开；fallback 明示 ArUco。旧营销表达已从权威文档移除。 |
| 20. 无物品框/轨迹也生成 placed | 当前规范路径只接受完整 movement，需连续检测、稳定前态、位移、新稳定态和媒体；普通调用或单帧不能创建。 |

## Mock 与真实测试的边界

### 保留的 Mock

- Vitest：API 错误格式、组件渲染、浏览器权限/Canvas、来源 badge、存储按钮；
- pytest：fake capture、monkeypatch、合成 observation、临时 SQLite；
- Playwright ui-mock：固定手机/沙发/95% 等界面契约。

它们有用，但只能报告为 Mock 单元/组件测试。尤其 `app.mock.spec.ts` 返回的媒体 body 是空字节，绝不能作为截图或录像证据。

### 核心非 Mock 路径

- `playwright.config.ts` 只匹配 `app.real.spec.ts`，在 8029 启动实际 FastAPI REAL 和独立 SQLite，验证无视频源 0 事件、无固定位置、刷新幂等、seed/virtual 拒绝；
- `playwright.demo.config.ts` 只匹配 `app.demo.spec.ts`，在 8030 启动实际 FastAPI DEMO，调用真实视频检测/状态机、读取 SQLite 结果并解码媒体；
- `playwright.mock.config.ts` 在 8031 只跑 UI 契约；
- `scripts/test.ps1` 强制 TEST 临时根，`-E2E` 运行 REAL config；DEMO E2E 需单独运行 `npm run test:e2e:demo`。

REAL E2E、DEMO 文件视频 E2E 与独立 UI Mock 使用不同入口。本阶段最终 Python 345 passed（0 失败/错误/跳过，JUnit 235.448 秒）包含 Mock 单元和实际后端/数据库集成；REAL E2E 2 passed / 6.975 秒，DEMO E2E 1 passed / 34.196 秒，二者均无失败、跳过或 flaky。实际退出码、媒体数量/哈希和分类证据以 `audit/p0-real/final-test-results.json` 与 [审计报告](AUDIT_REPORT.md) 为准。旧报告中的具体媒体大小和 skipped 数字不再冒充本阶段结果。即使 DEMO 的真实编码媒体能解码、哈希与 SQLite 一致，也不能证明物理摄像头识别。

## 关键词命中的非假数据用法

- `placeholder`：表单示例文字，不是自动提交值；
- `phone/sofa/桌面`：搜索建议、区域名称、测试 fixture 或 demo seed；
- `random`：安全码使用 `secrets`，普通 ID 使用 UUID；`Math.random` 只做 toast ID/cache bust；
- `fallback`：ArUco 算法降级、Windows API 兼容或 ESP32 备用网络申领，不返回固定事件；网络申领只创建未验证记录，仍需 REAL 本机 USB `OMREADY`；
- `setTimeout/setInterval`：超时、重连、UI 消息和帧泵，不构造历史记录；
- `placed/picked_up`：旧数据兼容类型、显示标签、负向断言或审计分类；规范状态机不再发这些独立历史行；
- `sample`：合成视频/标签和测试代码，当前都位于 DEMO/TEST 边界。

## 修复后的可信路径

```text
Start.bat
  -> OM_RUNTIME_MODE=REAL
  -> objectmemory.sqlite + real-media
  -> simulated source rejected
  -> continuous source session
  -> complete movement + decodable media + hashes
  -> movement_events / item_current_state
  -> REAL-scoped search
  -> explicit source/evidence badge
```

补充 fail-closed 分支：`browser_camera`、RTSP、ONVIF、MJPEG 在 REAL 只能更新“最后看到”，不能进入确认位置；`esp32_unverified` 不能绑定摄像头。连接成功或收到图像字节均不能单独证明物理硬件身份。

任何旧记录只要 `runtime_mode`、`source_type` 或 `is_simulated` 缺失，就绝不升级为 `real_verified`，并从默认 REAL 搜索排除；有明确确定性证据时审计器仍可保守分类为 demo、virtual 或 test。unknown 可以留在管理员审计输出供人工调查，但不能因媒体存在而升级为真实。

## 本轮确认与仍未确认

- 已确认：本阶段全文搜索、生产调用路径与规范 SQLite 只读结果已写入 `audit/p0-real/fake-data-search.json`；新增 P0 标记显示与工作流接口不能直接提交自动事件或通过结果。
- 已确认：两条本阶段实际 FastAPI/SQLite 防伪入口回归通过；历史清理数量保留在上面的明确历史小节，不与新的测试总数或磁盘快照混算。
- 编译结果只引用当前 [固件 manifest](../artifacts/firmware/manifest.json) 与 [硬件文档](HARDWARE.md)。当前日志为 26.03 秒编译成功；旧 12.40 秒不是本阶段结果，且二者都不是刷写证据。
- 没有可确认的真实 ESP32 或真机刷写证据；网络 claim、虚拟协议、USB 桥存在和编译成功不能互相替代。
- 仍未确认：物理手机静止、微调、遮挡、断流重连和十次跨区移动的完整 P0 套件；物理记录仍为 `NOT_RUN`。OpenCV 连续取帧和软件回归不能向上推断物理镜头或识别成功。
- 本阶段首次长跑的普通观察异常尚无原帧可供视觉复核；1 条未确认状态不能包装为真实确认事件，0 movement/媒体也不能掩盖当时两个失败门。第二次 1,800.125 秒长跑为 29/30，通过零状态/事件/媒体检查但正常退出失败；退出修复后的第三次 soak 1,800.094 秒（总观察 1,808.812 秒）为 30/30，零状态/事件/媒体、正常退出与摄像头释放均通过。失败原件仍保留，物理 14 轮仍为 `NOT_RUN`。
- 完整测试结果、失败项及长跑时长/帧数以本阶段 `audit/p0-real/final-test-results.json`、`audit/p0-real/runtime-stability.json`、保留的 `audit/p0-real/runtime-stability-first-attempt.json`、`audit/p0-real/runtime-stability-second-attempt.json` 和 [审计报告](AUDIT_REPORT.md) 为准；不复用前一阶段的旧测试数字。

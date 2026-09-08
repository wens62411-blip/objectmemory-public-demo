> 公开历史文档副本：本机身份信息已脱敏，私人数据库与实拍媒体未公开。历史结果不代表当前版本通过验收；当前限制请见 README。

# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: app.real.spec.ts >> 照片注册真实 UI → FastAPI → DINO → SQLite，不以合成纹理证明实物识别
- Location: tests\e2e\app.real.spec.ts:32:1

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByText('档案已就绪 · 实时摄像头尚未加载当前版本', { exact: true })
Expected: visible
Error: strict mode violation: getByText('档案已就绪 · 实时摄像头尚未加载当前版本', { exact: true }) resolved to 2 elements:
    1) <span class="badge badge-neutral">档案已就绪 · 实时摄像头尚未加载当前版本</span> aka getByRole('article').getByText('档案已就绪 · 实时摄像头尚未加载当前版本')
    2) <span class="badge badge-neutral">档案已就绪 · 实时摄像头尚未加载当前版本</span> aka getByLabel('合成纹理流程试验 · 照片与识别').getByText('档案已就绪 · 实时摄像头尚未加载当前版本')

Call log:
  - Expect "toBeVisible" with timeout 5000ms
  - waiting for getByText('档案已就绪 · 实时摄像头尚未加载当前版本', { exact: true })

```

# Page snapshot

```yaml
- generic [ref=e2]:
  - generic [ref=e3]:
    - link "跳转到主要内容" [ref=e4] [cursor=pointer]:
      - /url: "#main-content"
    - complementary [ref=e5]:
      - link "物忆首页" [ref=e7] [cursor=pointer]:
        - /url: /
        - generic [ref=e15]:
          - strong [ref=e16]: 物忆
          - generic [ref=e17]: 让每件物品，有迹可寻
      - navigation "主导航" [ref=e18]:
        - link "总览" [ref=e19] [cursor=pointer]:
          - /url: /
        - link "寻找物品" [ref=e24] [cursor=pointer]:
          - /url: /search
        - link "实时监控" [ref=e29] [cursor=pointer]:
          - /url: /live
        - link "事件时间线" [ref=e37] [cursor=pointer]:
          - /url: /events
        - link "物品管理" [ref=e42] [cursor=pointer]:
          - /url: /items
        - link "摄像头" [ref=e50] [cursor=pointer]:
          - /url: /cameras
        - link "设置" [ref=e55] [cursor=pointer]:
          - /url: /settings
      - group [ref=e60]:
        - generic "高级工具" [ref=e61] [cursor=pointer]
      - generic [ref=e64]:
        - generic [ref=e67]:
          - strong [ref=e68]: 本地服务正常
          - generic [ref=e69]: 视频不上传云端
        - link "连接手机 扫码打开，无需下载" [ref=e70] [cursor=pointer]:
          - /url: /settings#mobile-access
          - generic [ref=e73]:
            - strong [ref=e74]: 连接手机
            - generic [ref=e75]: 扫码打开，无需下载
    - generic [ref=e76]:
      - banner [ref=e77]:
        - link "连接手机" [ref=e78] [cursor=pointer]:
          - /url: /settings#mobile-access
        - generic [ref=e81]:
          - generic [ref=e82]: REAL · 真实模式
          - generic [ref=e83]: 本地处理
          - generic [ref=e84]: v0.1.0
      - main [ref=e85]:
        - generic [ref=e86]:
          - generic [ref=e87]:
            - generic [ref=e88]:
              - paragraph [ref=e89]: 物品管理
              - heading "教物忆认出你的东西" [level=1] [ref=e90]
              - paragraph [ref=e91]: 从一张照片开始，确认物品、建立本地识别档案，再用摄像头的新视角检验。
            - button "添加物品" [ref=e93] [cursor=pointer]
          - generic [ref=e100]:
            - strong [ref=e101]: 照片已保存，不等于已经能识别
            - generic [ref=e102]: 每件物品都能单独管理照片、确认目标框，并查看后台实际加载的档案版本。相似物品仍可能无法区分，现场测试会给出拒绝原因。
          - article [ref=e104]:
            - generic [ref=e105]: 合成
            - heading "合成纹理流程试验" [level=2] [ref=e107]
            - paragraph [ref=e108]: 手机
            - generic [ref=e109]:
              - generic [ref=e110]: 档案已就绪 · 实时摄像头尚未加载当前版本
              - paragraph [ref=e111]: 1 张参考照片 · 档案版本 2
            - button "管理照片 / 测试识别" [ref=e112] [cursor=pointer]
            - generic [ref=e121]:
              - generic [ref=e122]: 最后位置
              - strong [ref=e123]: 暂时没有真实摄像头产生的位置记录。
            - generic [ref=e124]:
              - link "查看历史" [ref=e125] [cursor=pointer]:
                - /url: /events?item_id=[LOCAL_ID]
              - button "编辑" [ref=e126] [cursor=pointer]
              - button "删除" [ref=e130] [cursor=pointer]
          - group [ref=e134]:
            - generic "高级诊断 · ArUco 标签" [ref=e135] [cursor=pointer]
          - dialog [ref=e136]:
            - generic [ref=e137]:
              - generic [ref=e138]:
                - heading "合成纹理流程试验 · 照片与识别" [level=2] [ref=e139]
                - paragraph [ref=e140]: 让照片真正参与本地识别；先确认目标，再用不同视角测试。
              - button "关闭" [ref=e141] [cursor=pointer]: ×
            - generic [ref=e142]:
              - list [ref=e143]:
                - listitem [ref=e144]: 1 添加照片
                - listitem [ref=e145]: 2 确认目标
                - listitem [ref=e146]: 3 建立档案
                - listitem [ref=e147]: 4 现场测试
              - status [ref=e148]: 已收到后台档案结果。只有下方显示当前版本已加载，才表示实时识别已启用。
              - generic [ref=e149]:
                - generic [ref=e150]:
                  - generic [ref=e151]:
                    - paragraph [ref=e152]: 01 · 参考照片
                    - heading "先给它一个清楚的近照" [level=3] [ref=e153]
                  - generic [ref=e154]: 1 张已保存
                - generic [ref=e155]:
                  - generic [ref=e156]:
                    - generic [ref=e157] [cursor=pointer]:
                      - text: 选择照片
                      - button "选择参考照片" [ref=e162]
                    - generic [ref=e163] [cursor=pointer]:
                      - text: 手机拍一张
                      - button "用手机拍摄参考照片" [ref=e167]
                    - generic [ref=e168]: 1 / 12 张
                  - paragraph [ref=e169]: 一张即可开始；之后建议补充 6–12 张正面、背面、侧面和实际摆放视角。每张最多 12 MB，仅 JPEG / PNG；照片保存在这台电脑，不上传云端。
                - generic [ref=e170]:
                  - generic [ref=e171]:
                    - generic [ref=e172]: 用于抓拍和测试的相机
                    - combobox "用于抓拍和测试的相机" [ref=e173]:
                      - option "请选择相机" [selected]
                  - button "从当前相机抓拍" [disabled] [ref=e174]
                - paragraph [ref=e178]:
                  - text: 只读取已启动相机的当前帧，不会擅自打开摄像头。
                  - link "去摄像头页面检查连接" [ref=e179] [cursor=pointer]:
                    - /url: /cameras
              - generic [ref=e180]:
                - generic [ref=e181]:
                  - generic [ref=e182]:
                    - paragraph [ref=e183]: 02 · 目标确认
                    - heading "告诉物忆，照片里要认的是哪一件" [level=3] [ref=e184]
                  - generic [ref=e185]: 1 / 1 张已确认
                - generic "已保存参考照片" [ref=e186]:
                  - generic [ref=e187]:
                    - button "选择参考照片 1" [pressed] [ref=e188] [cursor=pointer]:
                      - img "参考照片 1" [ref=e189]
                      - generic [ref=e190]: 已确认目标
                    - button "删除参考照片 1" [ref=e191] [cursor=pointer]
                - generic [ref=e195]:
                  - generic "拖动框选照片中的注册物品" [ref=e196]:
                    - img "待确认的原始参考照片" [ref=e197]
                    - generic: 注册物品
                  - paragraph [ref=e198]: 拖动框选，或使用下方数值调整。只保留目标本身，尽量避开手和背景；多个物体时，请明确选择正在注册的这一件。
                  - generic [ref=e199]:
                    - generic [ref=e200]:
                      - generic [ref=e201]: 左边距（%）
                      - spinbutton "左边距（%）" [ref=e202]: "10"
                    - generic [ref=e203]:
                      - generic [ref=e204]: 上边距（%）
                      - spinbutton "上边距（%）" [ref=e205]: "10"
                    - generic [ref=e206]:
                      - generic [ref=e207]: 宽度（%）
                      - spinbutton "宽度（%）" [ref=e208]: "80"
                    - generic [ref=e209]:
                      - generic [ref=e210]: 高度（%）
                      - spinbutton "高度（%）" [ref=e211]: "80"
                  - button "目标区域已确认" [disabled] [ref=e212]
                  - paragraph [ref=e215]: 确认只保存目标区域，不代表已经建立或加载识别档案。
              - generic [ref=e216]:
                - generic [ref=e217]:
                  - generic [ref=e218]:
                    - paragraph [ref=e219]: 03 · 识别档案
                    - heading "已保存 → 已建立 → 已加载" [level=3] [ref=e220]
                  - generic [ref=e221]: 档案已就绪 · 实时摄像头尚未加载当前版本
                - generic "本地模型准备和服务加载状态" [ref=e222]:
                  - generic [ref=e223]:
                    - heading "本地准备结果" [level=4] [ref=e224]
                    - generic [ref=e225]: 本地准备已完成
                    - paragraph [ref=e226]: 准备器最近记录：1/22 00:50:20。完成表示本地文件与准备检查通过，不等于当前摄像头可识别。
                  - generic [ref=e227]:
                    - heading "当前服务加载" [level=4] [ref=e228]
                    - generic [ref=e229]: 当前服务模型已加载
                    - paragraph [ref=e230]: onnx-community/dinov2-small · 8b1f705a3a7f6f062f6bdd21986c1583d3ef105d:onnx-fp32-cls-rgb-bicubic256-centercrop224-l2-v1
                    - paragraph [ref=e231]: 下载、准备器加载、当前服务加载和物品档案是不同步骤。刷新状态只读取结果，不会发起下载。
                - generic [ref=e232]:
                  - generic [ref=e233]:
                    - term [ref=e234]: 档案版本
                    - definition [ref=e235]: "2"
                  - generic [ref=e236]:
                    - term [ref=e237]: 后台加载版本
                    - definition [ref=e238]: "2"
                  - generic [ref=e239]:
                    - term [ref=e240]: 有效参考照片
                    - definition [ref=e241]: "1"
                  - generic [ref=e242]:
                    - term [ref=e243]: 实时加载相机数
                    - definition [ref=e244]: "0"
                - paragraph [ref=e245]: 模型：onnx-community/dinov2-small 8b1f705a3a7f6f062f6bdd21986c1583d3ef105d:onnx-fp32-cls-rgb-bicubic256-centercrop224-l2-v1。新添、删图或重新框选后，需要更新档案；仅保存图片不会自动开启识别。
                - generic [ref=e246]:
                  - button "建立 / 更新识别档案" [ref=e247] [cursor=pointer]
                  - button "刷新状态" [ref=e255] [cursor=pointer]
                - paragraph [ref=e261]: 建立成功后启用照片识别，标签仅用于诊断。后台会将新档案热加载到已运行的相机，不会重新打开镜头。
                - paragraph [ref=e262]: 档案已就绪，可现场测试；实时摄像头尚未加载当前档案，不表示已开始持续识别。
              - generic [ref=e263]:
                - generic [ref=e265]:
                  - paragraph [ref=e266]: 04 · 现场测试
                  - heading "换个视角，看看它能否认出来" [level=3] [ref=e267]
                - paragraph [ref=e268]: 把物品放到选定相机前，与参考照片保持不同角度或拍摄时间。测试直接读取当前帧，不会把上传照片作为测试画面。本面板不将一次匹配直接写成位置事件，持续识别仍遵循后台证据规则。
                - button "测试当前画面识别" [disabled] [ref=e269]
                - paragraph [ref=e277]: 请先选择一个已启动的相机。
  - generic [ref=e279]:
    - generic [ref=e283]: 物品信息已保存；识别状态请以照片档案为准。
    - button "关闭提示" [ref=e284] [cursor=pointer]
```

# Test source

```ts
  1   | import { expect, test, type APIRequestContext } from '@playwright/test'
  2   | import { createHash } from 'node:crypto'
  3   | import { execFile as execFileCallback } from 'node:child_process'
  4   | import { readFile, realpath, stat } from 'node:fs/promises'
  5   | import { basename, resolve, relative, isAbsolute } from 'node:path'
  6   | import { promisify } from 'node:util'
  7   | import { fileURLToPath } from 'node:url'
  8   | 
  9   | const execFile = promisify(execFileCallback)
  10  | const projectRoot = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../..')
  11  | 
  12  | async function optionalHash(path: string) {
  13  |   try { return createHash('sha256').update(await readFile(path)).digest('hex') } catch { return null }
  14  | }
  15  | 
  16  | async function sqliteCounts(database: string) {
  17  |   const code = "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print(json.dumps({t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('movement_events','item_current_state','tracks','event_media','source_sessions')}));c.close()"
  18  |   const { stdout } = await execFile(resolve(projectRoot, '.venv', 'Scripts', 'python.exe'), ['-c', code, database], { cwd: projectRoot })
  19  |   return JSON.parse(stdout) as Record<string, number>
  20  | }
  21  | 
  22  | async function json<T>(response: Awaited<ReturnType<APIRequestContext['get']>>) {
  23  |   if (!response.ok()) {
  24  |     const responseBody = await response.text()
  25  |     throw new Error(`${response.url()} 应返回成功响应，实际为 HTTP ${response.status()}：${responseBody}`)
  26  |   }
  27  |   return response.json() as Promise<T>
  28  | }
  29  | 
  30  | test.describe.configure({ mode: 'serial' })
  31  | 
  32  | test('照片注册真实 UI → FastAPI → DINO → SQLite，不以合成纹理证明实物识别', async ({ page, request }) => {
  33  |   await json(await request.get('/api/session'))
  34  |   const diagnostics = await json<{ database_path: string }>(await request.get('/api/system/diagnostics'))
  35  |   const before = await sqliteCounts(diagnostics.database_path)
  36  |   const python = resolve(projectRoot, '.venv', 'Scripts', 'python.exe')
  37  |   const { stdout: encoded } = await execFile(python, ['-B', '-c',
  38  |     "import base64,cv2,numpy as np;a=np.random.default_rng(27).integers(0,256,(256,320,3),dtype=np.uint8);print(base64.b64encode(cv2.imencode('.png',a)[1]).decode())"], { cwd: projectRoot })
  39  |   await page.goto('/items')
  40  |   await page.getByRole('button', { name: '添加物品', exact: true }).click()
  41  |   await page.getByLabel('物品名称', { exact: true }).fill('合成纹理流程试验')
  42  |   await page.getByRole('combobox', { name: '类型', exact: true }).selectOption({ label: '手机' })
  43  |   await page.getByLabel('选择参考照片', { exact: true }).setInputFiles({ name: 'generated-pipeline-only.png', mimeType: 'image/png', buffer: Buffer.from(encoded.trim(), 'base64') })
  44  |   await page.getByRole('button', { name: '保存物品', exact: true }).click()
  45  |   await expect(page.getByRole('button', { name: '确认这是我的物品', exact: true })).toBeEnabled()
  46  |   await page.getByRole('button', { name: '确认这是我的物品', exact: true }).click()
  47  |   await expect(page.getByRole('button', { name: '目标区域已确认', exact: true })).toBeVisible()
  48  |   const buildResponse = page.waitForResponse(response => response.url().endsWith('/profile/build') && response.request().method() === 'POST')
  49  |   await page.getByRole('button', { name: '建立 / 更新识别档案', exact: true }).click()
  50  |   expect((await buildResponse).status()).toBe(200)
  51  |   await expect(page.getByText('当前服务模型已加载', { exact: true })).toBeVisible()
> 52  |   await expect(page.getByText('档案已就绪 · 实时摄像头尚未加载当前版本', { exact: true })).toBeVisible()
      |                                                                          ^ Error: expect(locator).toBeVisible() failed
  53  |   const items = await json<Array<{ id: string; name: string }>>(await request.get('/api/items'))
  54  |   const item = items.find(value => value.name === '合成纹理流程试验')!
  55  |   expect(item).toBeTruthy()
  56  |   const profile = await json<{ registration_status: string; profile_version: number; loaded_camera_ids: string[]; model_id: string }>(await request.get(`/api/items/${item.id}/profile`))
  57  |   expect(profile.registration_status).toBe('ready')
  58  |   expect(profile.model_id).toBe('onnx-community/dinov2-small')
  59  |   expect(profile.loaded_camera_ids).toEqual([])
  60  |   const { stdout: vectors } = await execFile(python, ['-B', '-c',
  61  |     "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);r=c.execute('SELECT dimension,embeddings FROM item_recognition_profiles WHERE item_id=?',(sys.argv[2],)).fetchone();v=json.loads(r[1]);print(json.dumps({'dimension':r[0],'count':len(v),'norm':sum(x*x for x in v[0])**.5}));c.close()",
  62  |     diagnostics.database_path, item.id], { cwd: projectRoot })
  63  |   expect(JSON.parse(vectors)).toMatchObject({ dimension: 384, count: 1 })
  64  |   expect(JSON.parse(vectors).norm).toBeCloseTo(1, 5)
  65  |   expect(await sqliteCounts(diagnostics.database_path)).toEqual(before)
  66  |   await page.reload()
  67  |   await page.getByRole('article').filter({ has: page.getByRole('heading', { name: item.name, exact: true }) }).getByRole('button', { name: '管理照片 / 测试识别' }).click()
  68  |   await expect(page.getByRole('button', { name: '目标区域已确认', exact: true })).toBeVisible()
  69  |   await json(await request.delete(`/api/items/${item.id}`))
  70  |   console.log('[PHOTO_REAL_E2E] real HTTP + actual local model + persisted 384D; generated image only; no camera opened; no movement created')
  71  | })
  72  | 
  73  | test('真实 FastAPI + 独立 SQLite：REAL 无视频源始终是 0 事件', async ({ page, request }) => {
  74  |   const canonicalReal = resolve(projectRoot, 'data', 'database', 'objectmemory.sqlite')
  75  |   const canonicalHashBefore = await optionalHash(canonicalReal)
  76  |   const session = await json<{ authenticated: boolean; local: boolean }>(await request.get('/api/session'))
  77  |   expect(session).toMatchObject({ authenticated: true, local: true })
  78  |   const runtime = await json<{ runtime_mode: string; is_simulated: boolean }>(await request.get('/api/runtime-config'))
  79  |   expect(runtime).toMatchObject({ runtime_mode: 'REAL', is_simulated: false })
  80  | 
  81  |   const diagnostics = await json<{ database_path: string; runtime_mode: string }>(await request.get('/api/system/diagnostics'))
  82  |   expect(diagnostics.runtime_mode).toBe('REAL')
  83  |   const databasePath = await realpath(diagnostics.database_path)
  84  |   const isolatedRoot = await realpath(resolve(databasePath, '..', '..'))
  85  |   const temporaryRoot = await realpath(resolve(projectRoot, 'data', 'temporary'))
  86  |   const isolatedRelative = relative(temporaryRoot, isolatedRoot)
  87  |   expect(isolatedRelative.startsWith('..') || isAbsolute(isolatedRelative)).toBe(false)
  88  |   expect(basename(isolatedRoot)).toMatch(/^playwright-real-/)
  89  |   const databaseRelative = relative(isolatedRoot, databasePath)
  90  |   expect(databaseRelative.startsWith('..') || isAbsolute(databaseRelative)).toBe(false)
  91  |   expect(databasePath.endsWith('objectmemory.sqlite')).toBe(true)
  92  |   expect((await stat(databasePath)).size).toBeGreaterThan(0)
  93  |   expect(await sqliteCounts(databasePath)).toMatchObject({ movement_events: 0, item_current_state: 0, tracks: 0, event_media: 0 })
  94  | 
  95  |   const cameras = await json<unknown[]>(await request.get('/api/cameras'))
  96  |   const eventsBefore = await json<unknown[]>(await request.get('/api/events?limit=100'))
  97  |   expect(cameras).toHaveLength(0)
  98  |   expect(eventsBefore).toHaveLength(0)
  99  | 
  100 |   const storage = await json<{ runtime_mode: string; screenshot_size: number; clip_size: number; retention_policy: string }>(await request.get('/api/storage/status'))
  101 |   expect(storage).toMatchObject({ runtime_mode: 'REAL', screenshot_size: 0, clip_size: 0, retention_policy: 'MINIMAL' })
  102 | 
  103 |   await page.goto('/')
  104 |   await expect(page.getByText('REAL · 真实模式', { exact: true })).toBeVisible()
  105 |   await expect(page.getByText('暂时没有真实摄像头产生的位置记录。', { exact: true })).toBeVisible()
  106 |   await expect(page.getByText(/演示模式，当前事件不来自真实家庭摄像头/)).toHaveCount(0)
  107 | 
  108 |   await page.goto('/search')
  109 |   await page.getByLabel('寻找物品').fill('我的手机在哪里')
  110 |   await page.getByRole('button', { name: '帮我找' }).click()
  111 |   await expect(page.getByText('暂时没有真实摄像头产生的位置记录。', { exact: true })).toBeVisible()
  112 |   await expect(page.getByText(/桌面|沙发|91%|16:32/)).toHaveCount(0)
  113 | 
  114 |   await page.reload()
  115 |   await expect(page.getByText('暂时没有真实摄像头产生的位置记录。', { exact: true })).toBeVisible()
  116 |   expect(await json<unknown[]>(await request.get('/api/events?limit=100'))).toHaveLength(0)
  117 | 
  118 |   const seed = await request.post('/api/system/demo-seed')
  119 |   expect(seed.status()).toBe(409)
  120 |   expect((await seed.json()).detail).toMatch(/DEMO|演示种子/)
  121 |   const virtualDevice = await request.post('/api/virtual-device/start', { data: { source: 'video' } })
  122 |   expect(virtualDevice.status()).toBe(409)
  123 |   expect((await virtualDevice.json()).detail).toMatch(/DEMO|虚拟设备/)
  124 |   expect(await json<unknown[]>(await request.get('/api/events?limit=100'))).toHaveLength(0)
  125 |   expect(await sqliteCounts(databasePath)).toMatchObject({ movement_events: 0, item_current_state: 0, tracks: 0, event_media: 0 })
  126 | 
  127 |   await page.goto('/cameras?add=1')
  128 |   await expect(page.getByRole('dialog')).toBeVisible()
  129 |   await expect(page.getByRole('button', { name: /视频文件回放/ })).toHaveCount(0)
  130 |   await expect(page.getByText(/测试视频入口只在 DEMO 模式出现/)).toBeVisible()
  131 | 
  132 |   await page.goto('/devices')
  133 |   await expect(page.getByRole('heading', { name: 'Virtual ESP32-CAM' })).toHaveCount(0)
  134 |   await expect(page.getByText('真机刷写').first()).toBeVisible()
  135 |   await expect(page.getByText('未执行', { exact: true }).first()).toBeVisible()
  136 | 
  137 |   await page.goto('/storage')
  138 |   await expect(page.getByRole('heading', { name: '存储管理' })).toBeVisible()
  139 |   await expect(page.getByText('MINIMAL', { exact: true }).first()).toBeVisible()
  140 |   await expect(page.getByRole('heading', { name: '删除所有真实记录' })).toBeVisible()
  141 |   const finalEvents = await json<unknown[]>(await request.get('/api/events?limit=100'))
  142 |   const finalCounts = await sqliteCounts(databasePath)
  143 |   const finalStorage = await json<{ database_size: number; screenshot_size: number; clip_size: number; log_size: number }>(await request.get('/api/storage/status'))
  144 |   console.log(`[REAL_E2E_EVIDENCE] ${JSON.stringify({
  145 |     runtime_mode: runtime.runtime_mode,
  146 |     database_path: databasePath,
  147 |     database_size_bytes: (await stat(databasePath)).size,
  148 |     api_event_count: finalEvents.length,
  149 |     camera_count: cameras.length,
  150 |     sqlite_counts: finalCounts,
  151 |     storage: finalStorage,
  152 |   })}`)
```
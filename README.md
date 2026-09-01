# 微信朋友圈自动化系统

版本无关的 PC 微信朋友圈自动化。事件驱动架构 + 语义级定位 + 可持久化的多数据源 Agent。

[![Version](https://img.shields.io/badge/version-0.6.6-blue)]()
[![Tests](https://img.shields.io/badge/tests-191%20passed-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.10+-blue)]()
[![.NET](https://img.shields.io/badge/.NET-8.0-purple)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()

只使用公开的操作系统 API（截图、OCR、键鼠模拟、UIAutomation）驱动真实的微信桌面客户端 —— 不注入、不改协议、不依赖微信控件树，因此微信升级不会直接把自动化打挂。

---

## 两个并存的子系统

| 子系统 | 位置 | 职责 | 入口 |
|--------|------|------|------|
| **核心桌面引擎** | `src/core/` `src/locator/` `src/executor/` `src/monitor/` `src/recovery/` | 事件驱动的单次发布自动化，含定位、类人操作、风控、恢复 | `main.py`、`src/api/server.py` |
| **内容中心 Agent** | `src/agent/` | 常驻 Windows 服务：从远程数据源领取内容任务、下载媒体、调用核心引擎发布、把结果可靠回报 | `python main.py --agent` |

Agent 通过薄适配层 `DesktopPublishExecutor`（`src/agent/executor.py`）复用核心引擎，二者共享同一套安全边界。

## 快速开始

### 核心引擎（本地手工发布）

```bash
# 一键安装（创建 venv、装依赖、编译 C# UIA 微服务）
setup.bat

# 系统自检（只检查依赖/OCR/UIA/微信进程，不操作界面）
venv\Scripts\activate
python main.py --test

# 安全预览：准备好内容后停在编辑页，不点击“发表”
python main.py --text "今天天气真好" --images photo1.jpg

# 只有显式授权才会执行最终点击
python main.py --text "今天天气真好" --images photo1.jpg --confirm-publish

python main.py --interactive     # 交互模式
python main.py --schedule        # 定时调度（读取 API Server 里的 cron 任务）
python main.py --status          # 查看登录/风控/UIA 状态
```

### 内容中心 Agent（常驻，自动领任务）

```bash
# 启动 Agent；操作入口是原生“微信小助手”控制台
python main.py --agent

# 指定配置文件；不自动开浏览器（开机自启动用这个）
python main.py --agent --agent-config path/to/config.yaml --agent-no-browser
```

首次启动会在 `%LOCALAPPDATA%\WechatPublisherAgent\config.yaml` 生成配置并自动注册默认内容中心，随后在“微信小助手”原生控制台填入凭据即可开始工作。

## 安全边界（改代码前必读）

这是整个项目最重要的不变量，两个子系统各有一条：

1. **核心引擎默认不发布。** `main.py` 只把内容准备到编辑页就停下，只有传入 `--confirm-publish`（或 API 的 `confirm_publish: true`）才会点击“发表”。`PublishResult.stopped_before_publish` 用来区分“安全停下”和“失败”。
2. **Agent 的最终点击意图先落盘，且永不自动重试。** 点击前 `before_final_click` 把 `final_click_intent` 写入 SQLite 账本，点击后 `after_final_click` 把任务推进到 `confirming`。一旦意图已记录，崩溃或重启后该任务只会以 `uncertain` / `POST_CLICK_UNCONFIRMED` 上报，绝不重发 —— 宁可让人工确认，也不重复刷屏。

Agent 领单前要求桌面可交互且未锁屏、微信正在运行且已登录；朋友圈窗口无需常驻，领单后会在最终发布前自动打开并再次严格预检。

## 核心特性

| 特性 | 说明 |
|------|------|
| **版本无关** | OCR 文字语义 + SIFT/ORB 图形语义 + 锚点空间推算 + 模板像素匹配，四级降级，不依赖控件树 |
| **事件驱动** | `EventBus` + Watcher 后台监测，`bus.wait_for(...)` 等事件而非 `time.sleep()` |
| **类人行为** | Gamma 分布延迟 + 二次贝塞尔鼠标轨迹 + 逐字打字时序 + 随机多余动作 |
| **风控感知** | OCR 识别风控信号 + 指数退避冷却 + 每日操作上限 |
| **多层恢复** | 操作重试 → 策略降级 → 窗口恢复 → 进程恢复 → 放弃告警 |
| **多账号** | 按窗口发现所有微信实例，每个账号独立 Publisher/Calibrator/Operator/RiskDetector |
| **自动校准** | 启动时 OCR 扫描建立锚点；窗口移动/缩放自动重新校准 |
| **版本追踪** | 从 PE 头读取微信版本，变化时自动重建模板库 |
| **任务持久** | Agent 侧 SQLite 账本 + 事务化 outbox，重启不丢状态；核心侧 `state.json` 记录每日计数与历史 |
| **通知系统** | Telegram Bot + 邮件（SMTP）+ Windows 系统通知 |

### Agent 0.6.6 新增

- 兼容微信 4.x 无障碍控件树不暴露导航名称的情况：悬停视觉候选并用 OCR 读取中文 Tooltip。
- Tooltip“朋友圈”优先于历史模板名称，修复朋友圈直达图标被误判成“发现”的问题。
- 点击任何导航候选后先检查独立朋友圈窗口，仅在确认进入“发现”时继续查找二级入口。
- 导航诊断日志记录模板、置信度、Tooltip、动作与坐标，便于定位不同微信界面。

### Agent 0.6.5 新增

- 微信进程扫描改用 Windows 原生 `QueryFullProcessImageNameW`，不再为无权限 PID 反复启动 `tasklist.exe` 黑窗。
- 其他诊断和恢复命令统一使用无窗口子进程，避免桌面 Agent 拉起控制台窗口。

### Agent 0.6.4 新增

- 覆盖安装时由控制台安全关闭旧版后台后再启动新版，安装器不再并行拉起两个 Agent。
- 后台与控制台分别增加 Windows 单实例保护，重复启动不会产生窗口或任务领取循环。
- 控制台代启动后台时完整透传 `--agent-config`，避免自定义环境误连生产数据源。
- Agent 启动、状态刷新和心跳不再自动点击微信；微信昵称仅在用户点击“重新识别微信”后读取。
- 手动身份识别从最多六组坐标轮询改为单次 DPI 自适应点击，识别完成后只关闭已确认的资料卡。
- 微信窗口会话变化后立即清除旧身份缓存，必须重新识别，避免把任务路由到旧账号。
- PaddleOCR 初始化失败会在当前进程熔断，不再在扫描循环中反复加载模型和刷错误日志。

### Agent 0.6.2 新增

- 发布结果确认成功后只关闭独立的“朋友圈”窗口；安全预览、失败和结果不确定时保留窗口，便于人工检查。
- 常驻 `WeChatUIA.exe` 窗口监控改为无控制台启动，不再显示黑色辅助窗口。

### Agent 0.6.0 新增

- “微信小助手”原生控制台可编辑朋友圈文案、选择 1–9 张 JPG/PNG 图片和本机执行时间，创建一次性本地定时任务。
- 创建任务时冻结当前识别到的微信昵称/微信号；执行前再次核对，账号不一致时停止发布，避免误发。
- 图片创建时复制到 `%LOCALAPPDATA%\WechatPublisherAgent\data\local-media`，不依赖原文件后续是否移动或删除。
- 定时任务、执行状态和最终点击意图保存在现有 SQLite 账本中；点击意图落盘后重启不会自动重复发布。
- 待执行和点击前失败的任务可编辑并重新选择时间，也可在控制台取消。
- 安装应用、开始菜单、桌面快捷方式和安装包统一显示为“微信小助手”；内部路径与计划任务名保持不变，支持覆盖升级。

### Agent 0.5.0 新增

- Publisher Agent V2：同一台 Windows Agent 同时上报朋友圈桌面执行器和多个 WechatSync Chrome Profile。
- 精确路由键包含 `executorInstanceId + providerKey + platform + operation + profileId + accountStableId`；领取和执行前各校验一次，账号切换后拒绝执行。
- 知乎、掘金通过 WechatSync `v2` 扩展创建草稿，回传 `syncId/postId/postUrl/draftOnly`，不会把草稿误记成已发布。
- 每个 Chrome Profile 使用独立的 loopback WebSocket 端口与 DPAPI Token；Bridge 强制绑定 `127.0.0.1`，不暴露到局域网。
- V2 使用独立 SQLite 账本和事务 Outbox；调用平台前先记录 `final_action_intent`，崩溃后不自动重复创建草稿。
- 原生桌面控制台可新增/编辑 Profile、启动 Chrome、复制扩展连接配置、检测知乎/掘金登录账号。

### Agent 0.3.2

- 本机资料卡 OCR 识别当前登录微信的**昵称和微信号**，随各数据源心跳上报。
- 每个任务按 `agentId + accountKey` 精确领取；同一内容中心的其他设备不能领取指定账号的任务。
- 公共群发由内容中心展开为“每个微信号一条任务”，每个账号只领自己那一份。
- 支持多个 `standard-http-v1` 数据源，加权公平调度 + 按来源独立健康/退避状态。
- 来源凭据用当前 Windows 用户的 **DPAPI** 加密保存（配置里只存 `dpapi://` 引用，不存明文）。
- 首次启动自动注册默认内容中心，生产 URL 可在本机页面直接编辑并立即生效。
- 本机管理页提供「重新识别微信」和「安全退出 Agent」；发布执行中会拒绝退出，避免留下不确定状态。
- 新版应用图标用于程序、安装包和桌面快捷方式。

## 架构

```
                    ┌──────────────────────────────────────┐
Telegram/Discord ──▶│ OpenClaw ─┐                          │
Electron 前端    ──▶│           ├─▶ Python API Server :18080│
                    │           │   (REST + WebSocket)      │
远程内容中心     ──▶│ Agent ────┘                          │
  (HTTP 协议)       │  └─ 本机管理页 127.0.0.1:17821        │
                    └───────────────┬──────────────────────┘
                                    ▼
                        ┌───────────────────────┐
                        │   Core Engine          │
                        │                        │
                        │ EventBus               │
                        │  ├─ Watchers  OCR/UIA/ │
                        │  │             Window/ │
                        │  │             Timer   │
                        │  ├─ Publisher 状态机   │
                        │  ├─ Locators  OCR→SIFT │
                        │  │            →Anchor  │
                        │  │            →Template│
                        │  ├─ Monitors  Risk/弹窗│
                        │  └─ Recovery  五层恢复 │
                        └───────────┬───────────┘
                                    ▼
                     Windows 公开 API + C# UIA 微服务
                                    ▼
                              PC 微信客户端
```

### 发布状态机

```
IDLE → ENTERING_MOMENTS → TYPING_CONTENT → [ADDING_IMAGES]
     → CONFIRMING_PUBLISH → DONE
```

每步由事件推进（如点击“朋友圈”后等待 `text.appeared("这一刻的想法")`），失败走独立重试与风控冷却。

### Python + C# 分工

Python 负责 OCR / CV / 编排 / API；`src/cs_uia_service/` 是 .NET 8 控制台程序（net8.0-windows，self-contained 单文件），用原生 `System.Windows.Automation` 而非 FlaUI，以子进程方式用 JSON over stdout 通信，命令有 `dump-tree`、`check-login`、`activate`、`open-moments`、`monitor`、`get-rect`。**`WeChatUIA.exe` 缺失不是致命错误** —— 系统会降级为纯 OCR 模式，功能部分受限。

## CLI 参考

```bash
# ── 发布 ──
python main.py --text "文字内容" [--images a.jpg b.jpg]
python main.py --text "文字内容" --confirm-publish   # 显式授权最终点击
python main.py --text "文字内容" --dry-run           # 空跑，不加载 OCR 也不碰微信
python main.py --batch posts.txt                     # 每行: 文字|图片1 图片2

# ── 模式 ──
python main.py --interactive        # 交互模式（需输入 PUBLISH 才点发表）
python main.py --schedule           # cron 调度，任务来自 API Server
python main.py --status             # 登录/风控/UIA 状态
python main.py --resume             # 打印上次中断信息

# ── 多账号 ──
python main.py --accounts           # 列出所有微信窗口（含 PID）
python main.py --account <名称|PID> --text "..."

# ── 维护 ──
python main.py --test               # 自检
python main.py --calibrate          # 强制重新校准锚点
python main.py --extract-templates  # 提取/更新图标模板库
python main.py --verify-ocr-runtime # 仅验证打包版 OCR 运行时（打包流水线用）
python main.py --config path.yaml   # 指定核心引擎配置

# ── Agent ──
python main.py --agent [--agent-config path.yaml] [--agent-no-browser]
```

检测到多个微信窗口且未指定 `--account` 时会直接拒绝执行，避免发错账号。

## 内容中心 Agent

### 组成

| 模块 | 职责 |
|------|------|
| `app.py` `PublisherAgentApp` | 组装根：装配所有组件，跑 worker/heartbeat 线程与本机管理服务 |
| `worker.py` / `worker_v2.py` | V1 朋友圈兼容 worker 与通用 V2 worker；同一主循环串行执行，避免两个协议同时操作桌面 |
| `source_manager.py` | 每个来源一个适配器 + 独立健康/退避状态 + 加权公平调度（`scheduler.py`） |
| `ledger.py` / `ledger_v2.py` | V1/V2 SQLite 状态机，重启可续；按幂等键识别重复领取和动作意图 |
| `outbox.py` / `outbox_v2.py` | 事务化事件队列，带退避重试地回报给来源，**从不重跑桌面发布或草稿创建** |
| `connectors/registry.py` | 汇总 Windows 朋友圈执行器与所有 Browser Connector 的能力、账号和健康状态 |
| `connectors/wechatsync*.py` | 兼容 WechatSync v2 扩展协议的 loopback Bridge；执行 `checkAuth` / `syncArticle` |
| `media_cache.py` | 下载任务媒体，强制主机白名单（`mediaSecurity.allowedHosts`）与缓存上限；对受保护资源带来源凭据，但**绝不把凭据转发给重定向后的主机** |
| `environment.py` | 探测桌面/微信状态，准备与还原朋友圈窗口 |
| `wechat_identity.py` | OCR 识别本机登录微信的昵称与微信号 |
| `credential_store.py` | DPAPI 加密来源凭据（`dpapi://` 引用） |
| `executor.py` | `DesktopPublishExecutor`：包装核心引擎，实现最终点击回调 |

### 任务状态

```
claimed → executing → final_click_intent → confirming → succeeded
                   └────────────────────────────────┴──▶ failed / uncertain
```

`final_click_intent` 之后的失败一律归为 `uncertain`（`POST_CLICK_UNCONFIRMED`），不重试。

V2 草稿链路对应为：

```
claimed → executing → final_action_intent → completing → succeeded
                    └───────────────────────────────▶ uncertain
```

`final_action_intent` 在调用 WechatSync 前与 Outbox 事件同一事务落盘；此后进程退出只回报 `POST_ACTION_UNCONFIRMED`，不会再次调用平台。

### 配置示例

默认路径 `%LOCALAPPDATA%\WechatPublisherAgent\config.yaml`：

```yaml
schemaVersion: 1
agent:
  id: agent-3f9c12ab77d0
  displayName: WORKSTATION-01
  accountKey: wechat-main
runtime:
  heartbeatSeconds: 15        # 5-60
  defaultLeaseSeconds: 180    # 30-600
  pollSeconds: 2              # 1-30
  mediaCacheMaxMiB: 1024
  localAdminHost: 127.0.0.1   # 只能是回环地址
  localAdminPort: 17821
sources:
  - id: auto-content-production
    name: 智能内容运营平台
    type: standard-http-v2
    baseUrl: https://example.com/openapi/publisher-agent/v2  # 非回环必须 HTTPS
    enabled: true
    weight: 1                 # 1-10，加权公平调度
    accountKey: wechat-main
    auth:
      type: bearer            # bearer | api_key_header
      credentialRef: dpapi://auto-content-production
    mediaSecurity:
      allowedHosts: [example.com]   # 必须是精确主机名
      allowPrivateNetwork: false
wechatSyncProfiles:
  - id: chrome-default
    name: Chrome 默认内容账号
    enabled: true
    bridgeHost: 127.0.0.1           # 固定值，不能配置成 0.0.0.0
    bridgePort: 9527                 # 多 Profile 端口不能重复
    tokenRef: dpapi://wechatsync-chrome-default
    platforms: [zhihu, juejin]
    chromeExecutable: null           # 留空时自动发现 Chrome
    userDataDir: D:/BrowserProfiles/content-a
    profileDirectory: Default
    extensionPath: D:/Extensions/Wechatsync
    autoLaunch: false
```

配置模型 `extra="forbid"`，写错字段会直接报错；`baseUrl` 除回环外强制 HTTPS，`credentialRef` 必须是 `dpapi://`。

### 本机管理页与 API

只监听 `127.0.0.1:<localAdminPort>`（默认 17821），根路径 `/` 返回内置的单页管理界面，`/api/docs` 是 OpenAPI 文档。

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/health` | 存活探测 |
| GET | `/api/status` | Agent/微信/worker/来源/outbox/媒体缓存/告警的完整快照 |
| PATCH | `/api/identity` | 修改显示名与账号别名 |
| GET | `/api/sources` | 各来源状态与健康度 |
| POST | `/api/sources` | 新增来源（重复 id 返回 409） |
| PUT | `/api/sources/{id}` | 更新来源（含 URL、凭据） |
| DELETE | `/api/sources/{id}` | 删除来源及其本地凭据 |
| POST | `/api/sources/{id}/test` | 连通性与协议版本测试 |
| GET | `/api/connectors/wechatsync` | Profile、Bridge、登录账号与执行器状态 |
| POST/PUT/DELETE | `/api/connectors/wechatsync[/{id}]` | 新增、更新、删除 Chrome Profile 配置 |
| POST | `/api/connectors/wechatsync/{id}/test` | 强制刷新知乎/掘金登录状态 |
| POST | `/api/connectors/wechatsync/{id}/launch` | 按独立 user-data-dir 启动 Chrome |
| GET | `/api/connectors/wechatsync/{id}/token` | 本机确认后读取扩展 Token；只用于复制到扩展设置 |
| POST | `/api/preflight` | 桌面/微信/朋友圈窗口预检 |
| POST | `/api/wechat/identify` | 重新 OCR 识别微信身份 |
| POST | `/api/shutdown` | 安全退出（发布中返回 409） |
| GET | `/api/tasks` | 最近任务（默认 50 条） |
| GET | `/api/outbox` | 待发送事件积压量与最旧事件年龄 |

### WechatSync Profile 接入

1. 在 Agent 原生控制台的“浏览器发布账号”新增一个 Profile；同一平台的多个账号必须分别使用独立 `userDataDir` 和 Bridge 端口。
2. 在该 Profile 的 Chrome 中安装 WechatSync v2 扩展，分别登录知乎/掘金。
3. 选择 Profile，点击“复制连接配置”；在扩展 MCP 设置中填入 `ws://127.0.0.1:<端口>` 和 Token，然后开启 MCP 连接。
4. 点击“检测登录”。只有显示扩展已连接且列出平台昵称后，Agent 才会上报 `ready` 账号，内容中心才允许选择该账号创建草稿任务。

WechatSync 上游桥接默认可能监听所有网卡，本 Agent 没有复用该监听器，而是实现同协议的 loopback-only Bridge。扩展不得配置局域网 IP，也不要复用其他 Profile 的 Token。

`/api/wechat/identify` 和 `/api/shutdown` 需要请求头 `X-Local-Agent-Action: confirmed`，防止被本机页面之外的东西误触。

### 远程数据源协议

Agent 按来源配置分别使用 `sources/standard_http_v1.py` 或 `sources/standard_http_v2.py`，两版相对 `baseUrl` 使用相同资源路径：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/meta` | 协议握手，必须声明 `wechat-moments-publisher-source` 且版本含 `1.0` |
| POST | `/agents/heartbeat` | 上报 Agent/微信状态（含昵称、微信号、accountKeys、capabilities） |
| POST | `/tasks/claim` | 领取任务，`204` 表示当前无任务 |
| POST | `/tasks/{taskId}/lease/renew` | 续租 |
| POST | `/tasks/{taskId}/events` | 回报任务事件（幂等键 = `eventId`） |

V1 心跳只上报微信桌面快照；V2 心跳上报同一 Host 下的 `executors[]` 和 `accounts[]`，领单时携带当前精确路由集合。服务端返回的 V2 任务必须匹配 `executorInstanceId + providerKey + platform + operation + profileId + accountStableId`，否则 Agent 在最终动作前拒绝执行。V1/V2 字段 Schema 不可混用；已有 V1 来源不会被自动迁移。

认证按 `auth.type` 走 `Authorization: Bearer <secret>` 或自定义 `headerName`。

### 线格式契约

`contracts/publisher-agent/v1/`、`contracts/publisher-agent/v2/` 与 `auto-content` 中对应版本 **必须逐字节一致**。测试 `test_publisher_contracts.py` 会校验两版 Schema、fixture 和仓库镜像；Pydantic 模型只做防御性校验，不重新定义线格式（camelCase 别名靠 `model_dump(by_alias=True)`）。

## API Server（核心引擎）

```bash
python -m uvicorn src.api.server:app --host 127.0.0.1 --port 18080
```

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/publish` | 发布朋友圈（`confirm_publish` 控制最终点击） |
| GET | `/api/status` | 系统状态 |
| POST/GET | `/api/schedule` | 创建 / 列出定时任务 |
| DELETE | `/api/schedule/{id}` | 删除定时任务 |
| GET | `/api/history` | 发布历史（`limit` ≤ 500） |
| POST | `/api/templates/scan` | 重新扫描图标模板 |
| GET | `/api/logs` | 读取日志尾部（`lines` ≤ 1000） |
| GET | `/api/accounts` | 列出检测到的微信账号 |
| POST | `/api/accounts/{name}/publish` | 指定账号发布 |
| GET | `/health` | 健康检查 |
| WS | `/ws/events` | 实时事件流 |

## 三层集成

### OpenClaw — 消息平台远程控制

在 Telegram/WhatsApp/Discord 中：

```
/publish 今天天气真好 | photo.jpg
/status
/schedule add 0 9 * * * | 早安
/history
```

实现：`integrations/openclaw/wechat-moments-skill.ts`

### Electron 前端 — 基于 sparkle-ref

Dashboard + Composer + Schedule + History。集成指南：`integrations/sparkle-frontend/INTEGRATION.md`

### Agent 本机管理页

Agent 自带零依赖单页界面，用于配置数据源、查看状态、预检环境、安全退出，见上文。

## 环境要求

| 组件 | 要求 | 用途 |
|------|------|------|
| Windows | 10/11 x64，可交互且未锁屏的桌面会话 | 自动化前提 |
| Python | 3.10+ | 核心引擎与 Agent |
| .NET SDK | 8.0+（可选） | 编译 C# UIA 微服务；不装则降级为纯 OCR |
| 微信 | PC 版 3.9+ / 4.x | 目标应用 |
| Inno Setup 6 | 可选 | 编译安装包 |

Windows 微信 4.x 使用独立的「朋友圈」窗口。当前桌面流程**需要至少一张图片**才能打开编辑页。

## 安装

```bash
# 方式 1: 一键脚本
setup.bat

# 方式 2: 手动
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[full,agent]"
dotnet publish src/cs_uia_service/WeChatUIA.csproj -c Release -r win-x64 --self-contained true -o src/cs_uia_service/publish
python main.py --test
```

可选依赖分组见 `pyproject.toml`：`ocr` / `api` / `agent` / `test` / `packaging` / `full`。

## 打包与部署

```powershell
# 全流程：装依赖 → 跑测试 → 编译 C# UIA → PyInstaller → 校验打包版 OCR → Inno Setup
powershell -ExecutionPolicy Bypass -File scripts\build-agent.ps1
```

产物：`dist\WechatPublisherAgent\WechatPublisherAgent.exe`，安装包 `packaging\output\微信小助手-0.6.4-setup.exe`。

当前本地构建产物未包含 Authenticode 签名。正式跨电脑分发前应使用组织代码签名证书签名安装包，并重新记录签名后文件的 SHA-256。

安装器（`packaging/installer.iss`）以最低权限安装到 `%LOCALAPPDATA%\Programs\WechatPublisherAgent`，在当前用户的 `HKCU\...\Run` 中注册登录启动项 `WechatPublisherAgent`（参数 `--agent`），并创建开始菜单/桌面快捷方式。升级会清理旧版同名计划任务；卸载会移除登录启动项但**保留** `%LOCALAPPDATA%\WechatPublisherAgent` 下的账本与凭据，以免丢掉尚未回报的结果事件。

| 脚本 | 用途 |
|------|------|
| `scripts/build-agent.ps1` | 发布构建全流程 |
| `scripts/verify-installed-agent.ps1` | 验收已安装的 Agent（可要求微信就绪、校验来源 id） |
| `scripts/install-startup.ps1` / `remove-startup.ps1` | 注册 / 移除登录自启计划任务 |
| `scripts/generate-agent-icon.py` | 生成 `assets/agent-icon.{ico,png}` |

> `packaging/WechatPublisherAgent.spec` 里有若干不显眼但必要的修复（PaddleX `configs/**` 数据、OCR 依赖的 dist metadata、`mklml.dll`）。删掉任何一处，打包版 OCR 就会坏。

回滚与运维流程见 `docs/deployment-and-rollback.md`。

## 配置

**核心引擎** `config/settings.yaml`：

- `wechat` — 窗口类名/标题、安装路径、版本文件
- `ocr` — 引擎选择（`paddleocr` / `wechat_native` / `easyocr`）、PaddleOCR 参数、缓存 TTL
- `feature_matching` — ORB/SIFT、Lowe ratio、RANSAC 阈值
- `human_simulation` — 延迟分布、鼠标速度、贝塞尔偏移、打字 WPM
- `moments` — OCR 文字标签（版本无关的关键）、图标模板、超时
- `safety` — 每日上限、任务间隔、指数退避冷却基数、最大冷却
- `notifications` — Telegram / SMTP / 通知策略
- `logging` — 级别、轮转、保留

`state.json` 持久化每日计数与最近发布历史。

**Agent** 用独立的 YAML（见上文），数据根目录默认 `%LOCALAPPDATA%\WechatPublisherAgent`，运行时也可通过本机管理页改动并立即生效。

OCR 引擎优先级：微信原生 OCR（`wechat_native_ocr.py`）→ PaddleOCR → EasyOCR。

## 测试

```bash
python -m pytest              # 119 passed
python -m pytest tests/test_core.py -v
python -m pytest tests/test_core.py::test_name -v
python -m ruff check .        # ruff: E/F/W/I，line-length 100，忽略 E501
```

`tests/` 全部是带 mock 的单元测试，**不会碰真实微信或桌面**。主要套件：

| 套件 | 覆盖 |
|------|------|
| `test_core.py` `test_events.py` | EventBus 与发布状态机 |
| `test_agent_worker.py` `test_agent_ledger.py` `test_agent_sources.py` `test_agent_standard_http.py` | Agent 任务流、账本、来源适配器 |
| `test_agent_media.py` `test_agent_environment.py` `test_agent_executor.py` `test_agent_admin.py` `test_agent_config.py` | 媒体白名单、环境探测、执行适配、本机 API、配置校验 |
| `test_publish_safety.py` `test_publisher_contracts.py` | 安全边界与线格式契约 fixture |
| `test_desktop_flow.py` `test_integration.py` | 桌面流程与端到端编排 |
| `test_account_manager.py` `test_wechat_identity.py` `test_human_sim.py` `test_file_dialog.py` `test_ocr_compat.py` `test_ocr_region.py` | 多账号、身份识别、类人行为、文件对话框、OCR 兼容 |

## 文件结构

```
main.py                  CLI 主入口（含 --agent 分支）
config/settings.yaml     核心引擎配置
src/core/                事件驱动核心：events / watchers / publisher / account_manager
src/locator/             版本无关定位：ocr / feature / anchor / template / router / 资源提取
src/executor/            执行层：operator / human_sim / uia_bridge / file_dialog /
                         state_machine / version_detector / wechat_discovery
src/monitor/             监控层：risk_detector / popup_handler / notifier
src/recovery/            五层错误恢复
src/moments/             早期状态机版发布器（MomentsPublisher）
src/api/                 FastAPI Server（REST + WebSocket）
src/agent/               内容中心 Agent（admin/ 本机管理，sources/ 协议适配器）
src/cs_uia_service/      C# UIA 微服务（.NET 8，System.Windows.Automation）
contracts/               publisher-agent v1 线格式契约与 fixtures
packaging/               PyInstaller spec + Inno Setup 脚本
scripts/                 构建、验收、自启动、图标脚本
integrations/            OpenClaw skill + sparkle-ref 前端集成
docs/                    部署与回滚运维文档
tests/                   119 个单元测试
```

`integrations/frida/` 是早期协议研究留下的独立 hook 脚本，**不被自动化流程引用**，也不参与打包。

## 参考文档

| 文档 | 内容 |
|------|------|
| `DEVELOPMENT.md` | 详尽设计文档：设计哲学、事件类型、Watcher、状态机、配置参考、排错 |
| `ARCHITECTURE.md` | 集成架构（OpenClaw / API / Electron） |
| `docs/deployment-and-rollback.md` | Agent 部署与回滚运维手册 |
| `integrations/sparkle-frontend/INTEGRATION.md` | Electron 前端如何挂载 `/wechat` 页面 |
| `contracts/publisher-agent/README.md` | 线格式契约说明 |
| `CLAUDE.md` | 给 AI 编码助手的仓库约定 |

文档、代码注释与 CLI 输出统一使用**中文**（保留英文技术名词），新增内容请沿用这一约定。

## 免责声明

本项目仅供技术研究和学习使用。使用自动化工具操作微信可能违反《腾讯微信软件许可及服务协议》，使用者需自行承担风险。

## License

MIT

# 开发文档

## 目录

1. [设计哲学](#1-设计哲学)
2. [系统架构](#2-系统架构)
3. [事件驱动机制](#3-事件驱动机制)
4. [核心模块详解](#4-核心模块详解)
5. [API Server](#5-api-server)
6. [OpenClaw 集成](#6-openclaw-集成)
7. [Electron 前端集成](#7-electron-前端集成)
8. [数据流与状态转换](#8-数据流与状态转换)
9. [关键技术决策](#9-关键技术决策)
10. [环境搭建与编译](#10-环境搭建与编译)
11. [配置文件说明](#11-配置文件说明)
12. [扩展指南](#12-扩展指南)
13. [故障排查](#13-故障排查)

---

## 1. 设计哲学

### 1.1 核心原则

```
不做的事                          要做的事
────────────────────────────────────────────────────
❌ 锁定微信版本                    ✅ 版本无关（文字语义 + 图形语义）
❌ 锁定屏幕 DPI/分辨率             ✅ OCR 和 SIFT 均无此依赖
❌ 注入微信进程（Hook）             ✅ 操作系统公开 API 操作
❌ 破解通信协议                     ✅ 纯视觉 + UIAutomation
❌ 依赖控件树（UIA 隐藏时）         ✅ OCR + 特征匹配 多级 Fallback
❌ time.sleep() 固定延迟            ✅ 事件驱动（等事件，不等时间）
❌ 轮询 OCR 扫描                    ✅ Watcher 后台监测 + EventBus 发布
```

### 1.2 三种语义定位

传统 GUI 自动化的核心问题是**版本相关的定位依赖**。本系统通过三个语义维度实现版本无关：

| 语义维度 | 定位策略 | 覆盖场景 | 版本相关性 |
|----------|----------|----------|-----------|
| **文字语义** | OCR 找文字标签（"朋友圈"永远是"朋友圈"） | 80% 带文字的按钮/标签 | 无 |
| **图形语义** | SIFT/ORB 特征点匹配图标形状 | 15% 纯图标的按钮 | 无（缩放/旋转/光照不变） |
| **空间语义** | 多锚点相对定位推断未知元素 | 5% 输入框等无标签元素 | 低（窗口尺寸变化时自动校准） |

### 1.3 事件驱动 vs 传统轮询

```
轮询驱动的浪费：
  click → sleep(1.5s) → OCR扫描 → sleep(0.5s) → OCR扫描 → 找到了
  时间线：[操作系统已在0.8s完成加载，但我们还在sleep等轮询间隔]

事件驱动的精准：
  click → wait_for(text.appeared("这一刻的想法")) → 继续
  时间线：零CPU空转，响应时间=事件发生时间
```

---

## 2. 系统架构

### 2.1 五层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      交互层                                     │
│  Telegram / WhatsApp / Discord ←→ Electron Desktop App          │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                      API 层 (FastAPI)                           │
│  POST /api/publish  GET /api/status  WS /ws/events             │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                      事件驱动核心层                             │
│  EventBus + WatchManager (OCRTextWatcher, UIATreeWatcher,       │
│  WindowWatcher, TimerWatcher)                                   │
└───────┬───────────────┬───────────────┬─────────────────────────┘
        │               │               │
┌───────▼──────┐ ┌──────▼───────┐ ┌─────▼──────┐
│   定位层      │ │   执行层      │ │  监控层    │
│ OCR 定位     │ │ 类人延迟(Gamma)│ │ 风控检测   │
│ SIFT/ORB匹配  │ │ 贝塞尔鼠标轨迹│ │ 弹窗清理   │
│ 锚点相对定位  │ │ 键盘时序模拟  │ │ 指数退避   │
│ 策略路由      │ │ C# UIA 桥接   │ │            │
│ PE 资源提取   │ │ 文件对话框     │ │            │
│ Protobuf OCR  │ │               │ │            │
└───────┬──────┘ └──────┬───────┘ └─────┬──────┘
        └───────────────┼───────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────────┐
│                      恢复层                                     │
│  多层级异常恢复（定位降级 → 窗口激活 → 进程重启）               │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 进程架构

```
┌─────────────────┐     JSON/stdout     ┌──────────────────┐
│   Python 主控    │◄───────────────────►│  C# UIA 微服务    │
│                  │   subprocess 调用   │  (WeChatUIA.exe)  │
│ · 流程编排      │                    │                  │
│ · OCR/CV 处理   │                    │ · 控件树 dump    │
│ · 类人模拟      │                    │ · 窗口监控       │
│ · 风控策略      │                    │ · 无障碍触发     │
│ · 事件总线      │                    │ · 登录状态检测   │
│ · FastAPI 服务  │                    │                  │
└────────┬────────┘                    └────────┬─────────┘
         │                                      │
         │         操作系统公开 API              │
         └──────────────┬──────────────────────-┘
                        │
              ┌─────────▼─────────┐
              │   微信客户端        │
              │  (WeChat.exe)      │
              └───────────────────┘
```

### 2.3 集成架构

```
用户交互层:
  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐
  │ Telegram │  │ WhatsApp │  │ Discord  │  │ Electron Desktop │
  │ /publish │  │ /publish │  │ /publish │  │ (sparkle-ref)    │
  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬─────────┘
       │              │             │                  │ IPC invoke
       └──────────────┼─────────────┘                  │
                      │                                │
              ┌───────▼────────┐              ┌────────▼────────┐
              │   OpenClaw     │   HTTP API   │ Electron Main   │
              │   Gateway      │◄────────────►│ (IPC handlers)  │
              │                │              │                 │
              │ WeChat Moments │              │ 转发到 Python   │
              │ Skill          │              │ API Server      │
              └───────┬────────┘              └────────┬────────┘
                      │                                │
                      │         HTTP REST               │
                      └───────────┬────────────────────┘
                                  │
              ┌───────────────────▼────────────────────┐
              │        Python API Server (FastAPI)     │
              │  REST + WebSocket (localhost:18080)    │
              └───────────────────┬────────────────────┘
                                  │
              ┌───────────────────▼────────────────────┐
              │           Core Engine                  │
              │  EventDrivenPublisher + Locators       │
              └────────────────────────────────────────┘
```

---

## 3. 事件驱动机制

### 3.1 EventBus 核心 API

```python
from src.core.events import EventBus, Event, EventType, global_event_bus

bus = EventBus()

# ── 订阅 ──
bus.on(EventType.TEXT_APPEARED, lambda e: handle(e))

# ── 一次性订阅（触发后自动取消） ──
bus.once(EventType.WINDOW_MOVED, lambda e: recalibrate())

# ── 通配符订阅（接收所有事件） ──
bus.on_any(lambda e: log_event(e))

# ── 发布 ──
bus.emit(Event(EventType.TEXT_APPEARED, "OCRWatcher", {
    'text': '已发送', 'matched': '已发送', 'x': 500, 'y': 600, 'confidence': 0.97
}))

# ── 等待事件（事件驱动替代 time.sleep） ──
event = bus.wait_for(EventType.TEXT_APPEARED,
                     payload_match={'matched': '已发送'},
                     timeout=10.0)

# ── 等待任意事件 ──
event = bus.wait_any([EventType.TEXT_APPEARED, EventType.TIMER_EXPIRED], timeout=15.0)
```

### 3.2 事件类型全集

| 事件类型 | 触发源 | 携带数据 | 用途 |
|----------|--------|----------|------|
| `element.appeared` | UIA Tree Watcher | name, x, y, controlType | 控件出现 |
| `element.vanished` | UIA Tree Watcher | name | 控件消失 |
| `text.appeared` | OCR Text Watcher | text, matched, x, y, confidence | 文字出现 |
| `text.vanished` | OCR Text Watcher | text, matched | 文字消失 |
| `window.moved` | Window Watcher | left, top, dx, dy | 窗口移动 |
| `window.resized` | Window Watcher | width, height, dw, dh | 窗口缩放 |
| `window.minimized` | Window Watcher | — | 窗口最小化 |
| `login.lost` | Login Check | detectedPage | 掉线 |
| `risk.warning` | Risk Detector | cooldown, signal | 风控警告 |
| `risk.critical` | Risk Detector | signal | 风控严重 |
| `risk.cooldown.start` | Risk Detector | duration | 冷却开始 |
| `risk.cooldown.end` | Risk Detector | — | 冷却结束 |
| `popup.detected` | Popup Handler | name, type | 检测到弹窗 |
| `popup.dismissed` | Popup Handler | name | 弹窗已关闭 |
| `step.started` | Publisher | step, task_id | 步骤开始 |
| `step.completed` | Publisher | step, elapsed | 步骤完成 |
| `step.failed` | Publisher | step, error, attempt | 步骤失败 |
| `step.retry` | Publisher | step, attempt, max | 步骤重试 |
| `publish.confirmed` | Publisher | task_id, elapsed | 发布确认 |
| `upload.complete` | OCR Watcher | — | 上传完成 |
| `upload.failed` | OCR Watcher | reason | 上传失败 |
| `timer.expired` | Timer Watcher | reason | 定时器到期 |
| `system.error` | Any | error, traceback | 系统错误 |
| `system.shutdown` | Publisher | — | 系统关闭 |

### 3.3 Watcher 事件源

| Watcher | 监测方式 | 间隔 | 发布的事件 |
|---------|----------|------|-----------|
| OCRTextWatcher | OCR 扫描 → diff 当前帧 vs 上一帧 | 1.0s | text.appeared, text.vanished |
| UIATreeWatcher | UIA dump → diff 控件名集合 | 0.5s | element.appeared, element.vanished |
| WindowWatcher | SetWinEventHook 系统回调 | 零轮询 | window.moved, window.resized, window.minimized |
| TimerWatcher | threading.Timer 到期 | 按需 | timer.expired |

### 3.4 事件驱动的发布流程

```
IDLE
  │  emit(STEP_STARTED)
  ▼
ENTERING_MOMENTS
  │  click("朋友圈")
  │  wait_for(TEXT_APPEARED, matched="这一刻的想法", timeout=5s)
  │
  ├─ 事件到达 → 进入下一步
  └─ 超时     → 清理弹窗 → 重试 (最多 3 次)
  ▼
TYPING_CONTENT
  │  type(text)
  │  wait_for(TIMER_EXPIRED, reason="typing_complete", timeout=2s)
  ▼
ADDING_IMAGES (有图片时)
  │  粘贴图片 → OCR 检测上传状态
  │
  ├─ 无"上传失败"文字 → 完成
  └─ 有"上传失败"     → 重试
  ▼
CONFIRMING_PUBLISH
  │  click("发表")
  │  wait_any([TEXT_APPEARED, TIMER_EXPIRED], timeout=15s)
  │
  ├─ "已发送" appeared → DONE
  └─ 超时/"失败"      → 重试 (最多 3 次)
  ▼
DONE
```

---

## 4. 核心模块详解

### 4.1 文件结构

```
wechat-moments-automation/
├── README.md                           # 项目说明
├── DEVELOPMENT.md                      # 本文件
├── ARCHITECTURE.md                     # 集成架构设计
├── LICENSE                             # MIT
├── requirements.txt                    # Python 依赖
├── main.py                             # 入口
├── config/
│   └── settings.yaml                   # 全局配置
├── src/
│   ├── core/                           # 事件驱动核心
│   │   ├── events.py                   #   EventBus + Event + EventType
│   │   ├── watchers.py                 #   Watcher 事件源
│   │   └── publisher.py                #   EventDrivenPublisher
│   ├── api/
│   │   └── server.py                   #   FastAPI REST + WebSocket
│   ├── locator/                        # 定位层（7 模块）
│   │   ├── ocr_locator.py              #   OCR 文字定位
│   │   ├── feature_locator.py          #   SIFT/ORB 特征匹配
│   │   ├── anchor_locator.py           #   锚点校准 + 相对定位
│   │   ├── router.py                   #   策略路由
│   │   ├── template_extractor.py       #   运行时模板提取
│   │   ├── resource_extractor.py       #   PE 资源提取 + Hook 捕获
│   │   └── wechat_native_ocr.py        #   微信 OCR protobuf 集成
│   ├── executor/                       # 执行层（5 模块）
│   │   ├── human_sim.py                #   类人行为模拟
│   │   ├── state_machine.py            #   状态机
│   │   ├── operator.py                 #   操作执行器
│   │   ├── uia_bridge.py               #   Python↔C# UIA 桥接
│   │   └── file_dialog.py              #   文件对话框自动化
│   ├── monitor/                        # 监控层（2 模块）
│   │   ├── risk_detector.py            #   风控检测 + 指数退避
│   │   └── popup_handler.py            #   弹窗检测 + 自动清理
│   ├── recovery/                       # 恢复层
│   │   └── error_recovery.py           #   多层级异常恢复
│   ├── moments/                        # 业务层
│   │   └── publisher.py                #   轮询版发布器（兼容）
│   └── cs_uia_service/                 # C# UIA 微服务
│       ├── WeChatUIA.csproj            #   .NET 项目文件
│       └── Program.cs                  #   FlaUI + WinEventHook
├── integrations/                       # 外部集成
│   ├── openclaw/
│   │   └── wechat-moments-skill.ts     #   OpenClaw Skill
│   └── sparkle-frontend/               #   Electron 前端
│       ├── INTEGRATION.md              #   集成指南
│       └── src/
│           ├── utils/wechat-ipc.ts     #    IPC 封装
│           ├── components/sider/
│           │   └── wechat-card.tsx     #    侧边栏卡片
│           ├── pages/
│           │   └── wechat-moments.tsx  #    朋友圈管理页面
│           └── main-ipc.ts             #    主进程 IPC 处理器
├── templates/
│   └── icons/                          # 图标模板
└── logs/                               # 运行日志
```

### 4.2 定位层

#### OCRLocator (`ocr_locator.py`)

主力定位手段，覆盖 80% 场景。

```python
locator = OCRLocator(engine='paddleocr')
locator.click_text("朋友圈")         # 找到并点击
matches = locator.find_text("发表")   # 搜索全部匹配
best = locator.find_best("发表")     # 取置信度最高
result = locator.wait_text("已发送", timeout=10.0)  # 等待出现
all_text = locator.get_all_text()    # 全屏文字调试
```

引擎选择：微信原生 OCR (WeChatOCR.exe + protobuf) → PaddleOCR → EasyOCR

#### FeatureLocator (`feature_locator.py`)

覆盖纯图标按钮，SIFT/ORB 特征匹配。

| 算法 | 精度 | 速度 | 专利 |
|------|------|------|------|
| SIFT | 最高 | 200-500ms | 需 opencv-contrib |
| ORB（默认） | 高 | 50-200ms | 无（开源） |

#### AnchorCalibrator (`anchor_locator.py`)

运行时自动校准，构建坐标映射。

```
启动时扫描:
  → OCR 扫导航栏 → nav_聊天, nav_通讯录, nav_朋友圈
  → 特征匹配定位图标 → icon_camera, icon_photo
  → 进入朋友圈页面扫描 → moments_这一刻的想法, moments_发表
  → 输出 CoordinateMapping
```

#### LocateRouter (`router.py`)

四级降级定位策略：

```
策略 1: OCR 文字定位     → 最快最准
策略 2: SIFT/ORB 特征匹配 → 纯图标元素
策略 3: 锚点相对定位     → 推断未知元素
策略 4: 像素模板匹配     → 兜底（版本敏感）
全失败  → 截图 + 日志    → 调试用
```

### 4.3 执行层

#### HumanSimulator (`human_sim.py`)

| 行为 | 算法 | 关键参数 |
|------|------|----------|
| 操作延迟 | `Gamma(shape=3) + uniform(0.7, 1.3)` | 5% 概率"走神" ×1.5~3 |
| 鼠标轨迹 | 二次贝塞尔 `(1-t)²P₀ + 2(1-t)tP₁ + t²P₂` | ±80px 控制点偏移, ±2px 抖动 |
| 键盘输入 | 逐字符，高频字 0.3× 基础延迟，标点 2.5-4.5× | WPM=60-100 |
| 多余动作 | 30% 概率插入 | 晃鼠标/看向别处/犹豫/滚轮 |

鼠标速度曲线：`t<0.2` 加速 → `0.2~0.8` 匀速 → `t>0.8` 减速逼近

#### UIABridge (`uia_bridge.py`)

Python ↔ C# 微服务通信层：

```python
bridge = UIABridge()
tree = bridge.dump_tree()              # 完整控件树 JSON
bridge.activate_window()               # 激活微信
login = bridge.check_login()           # 登录状态检测
bridge.start_window_monitor(callback)  # 窗口位置监控
```

#### FileDialogHandler (`file_dialog.py`)

图片添加三策略：

```
策略 A: CF_DIB 剪贴板图片 → Ctrl+V     (最优，绕过对话框)
策略 A2: CF_HDROP 文件路径 → Ctrl+V     (多图场景)
策略 B: pywinauto 操控文件对话框         (标准方案)
策略 C: pynput SendKeys 键盘模拟        (兜底)
```

### 4.4 监控层

#### RiskDetector (`risk_detector.py`)

风控信号（通过 OCR 文字匹配，版本无关）：

| 信号 | 等级 | 自动处理 | 冷却倍数 |
|------|------|----------|----------|
| "重新登录" | CRITICAL | stop | ×8 |
| "账号已被限制" | DANGER | stop | ×10 |
| "操作太频繁" | WARNING | pause | ×2 |
| "安全验证" | DANGER | stop | ×6 |
| "版本过低" | CRITICAL | stop | 需手动升级 |

指数退避冷却：第 N 次触发 → 冷却 `base * multiplier * 2^(N-1)` 分钟，上限 6 小时。

### 4.5 C# UIA 微服务 (`cs_uia_service/`)

#### 命令接口

| 命令 | 输出 | 用途 |
|------|------|------|
| `dump-tree` | 完整控件树 JSON | Python 获取 UI 结构 |
| `activate` | `{"success": true}` | 激活微信窗口 |
| `monitor` | 持续输出窗口信息 JSON | 窗口位置监控 |
| `check-login` | 登录状态 JSON | 掉线检测 |
| `get-window-rect` | 窗口矩形 JSON | 获取位置尺寸 |

#### 编译

```bash
cd src/cs_uia_service
dotnet restore
dotnet publish -c Release -o publish
# 输出: publish/WeChatUIA.exe
```

依赖：.NET 8.0 SDK + NuGet: FlaUI.UIA3, System.Text.Json

---

## 5. API Server

### 5.1 端点一览

| Method | Path | 说明 | 请求体 | 响应 |
|--------|------|------|--------|------|
| POST | `/api/publish` | 发布朋友圈 | `{text, images}` | `{success, task_id, elapsed_seconds, step_times, error}` |
| GET | `/api/status` | 系统状态 | — | `{status, wechat, risk, daily, templates_count, uptime_seconds}` |
| POST | `/api/schedule` | 创建定时任务 | `{text, images, cron, enabled}` | `{id, text, cron, enabled, next_run}` |
| GET | `/api/schedule` | 查看定时任务 | — | `[{id, text, cron, enabled, next_run}]` |
| DELETE | `/api/schedule/{id}` | 取消定时任务 | — | `{success}` |
| GET | `/api/history` | 发布历史 | `?limit=50` | `[{task_id, text, success, elapsed_seconds, timestamp}]` |
| POST | `/api/templates/scan` | 扫描模板 | — | `{success, count}` |
| GET | `/api/logs` | 运行日志 | `?lines=100` | `{file, lines: [...]}` |
| WS | `/ws/events` | 实时事件流 | — | 持续推送事件 JSON |
| GET | `/health` | 健康检查 | — | `{status: "ok"}` |

### 5.2 启动

```bash
pip install fastapi uvicorn pydantic croniter
python -m uvicorn src.api.server:app --host 127.0.0.1 --port 18080
```

### 5.3 WebSocket 事件格式

```json
{
  "type": "text.appeared",
  "source": "OCRTextWatcher",
  "payload": {
    "text": "已发送",
    "matched": "已发送",
    "x": 500,
    "y": 600,
    "confidence": 0.97
  },
  "timestamp": 1718123456.789
}
```

---

## 6. OpenClaw 集成

### 6.1 Skill 命令

在 Telegram/WhatsApp/Discord 中可用命令：

| 命令 | 说明 | 示例 |
|------|------|------|
| `/publish` | 发布朋友圈 | `/publish 今天天气真好 \| photo.jpg` |
| `/status` | 查看系统状态 | `/status` |
| `/schedule add` | 创建定时任务 | `/schedule add 0 9 * * * \| 早安` |
| `/schedule list` | 定时任务列表 | `/schedule list` |
| `/schedule remove` | 取消定时任务 | `/schedule remove task_001` |
| `/history` | 发布历史 | `/history` |

### 6.2 自然语言意图

OpenClaw LLM 识别的意图模式：

```
"发朋友圈" "发布朋友圈" "帮我发朋友圈" "发一条朋友圈"
    → 提取文字内容 → 调用 /api/publish

"朋友圈状态" "发布状态" "系统状态"
    → 调用 /api/status
```

### 6.3 安装

1. 将 `integrations/openclaw/wechat-moments-skill.ts` 放入 OpenClaw skills 目录
2. 设置环境变量 `WECHAT_MOMENTS_API=http://127.0.0.1:18080`
3. 重启 OpenClaw Gateway

---

## 7. Electron 前端集成

### 7.1 技术栈兼容

前端代码使用与 sparkle-ref 完全一致的技术栈：

| 技术 | sparkle-ref | 前端代码 | 匹配 |
|------|------------|----------|:----:|
| React | 19.2.6 | 19.x | ✅ |
| UI 组件 | @heroui/react + @heroui-v3/react | 同 | ✅ |
| CSS | Tailwind CSS 4.3 | 同 | ✅ |
| 图标 | react-icons 5.6 | 同 | ✅ |
| 路由 | react-router-dom 7.15 | 同 | ✅ |
| 拖拽 | @dnd-kit | useSortable | ✅ |
| 数据 | useSWR | 同 | ✅ |
| 配置 | useAppConfig | 同 | ✅ |
| IPC | ipcRenderer.invoke | ipcErrorWrapper | ✅ |

### 7.2 文件清单

```
integrations/sparkle-frontend/
├── INTEGRATION.md                           # 精确集成步骤
├── src/
│   ├── utils/wechat-ipc.ts                  # IPC 封装（模式匹配 ipc.ts）
│   ├── components/sider/wechat-card.tsx     # 侧边栏卡片（模式匹配 proxy-card.tsx）
│   ├── pages/wechat-moments.tsx             # 主页面（模式匹配 proxies.tsx）
│   └── main-ipc.ts                          # 主进程 IPC 处理器
```

### 7.3 sparkle-ref 需要修改的 3 个文件

| 文件 | 改动内容 | 代码量 |
|------|----------|--------|
| `src/renderer/src/routes/index.tsx` | 加 1 行 import + 1 个 route 对象 | 2 行 |
| `src/renderer/src/App.tsx` | 加 1 个 import, componentMap/siderCardRouteMap/defaultSiderOrder 各加 1 项 | 4 行 |
| `src/main/index.ts` | 加 1 行 import + 1 行 `registerWechatIpcHandlers()` | 2 行 |

总计：sparkle-ref 原有代码只需新增 8 行。

### 7.4 页面结构

```
/wechat → WechatMoments 页面
  ├── Tab "仪表盘"  — 微信状态 + 风控等级 + 今日统计 + 最近发布
  ├── Tab "编辑"    — 文字输入 + 立即发布/定时发布
  ├── Tab "历史"    — 发布记录列表（成功/失败标识 + 耗时）
  └── Tab "定时"    — Cron 表达式 + 预设 + 任务列表管理
```

---

## 8. 数据流与状态转换

### 8.1 初始化流程

```
main.py → publisher.initialize()
  ├─ find_wechat_window()         # win32gui.FindWindow
  ├─ ensure_window_active()       # SetForegroundWindow
  ├─ check_login_state()          # UIA / OCR 双重检测
  ├─ calibrator.calibrate()       # OCR + SIFT 建立锚点
  ├─ WatchManager.start_all()     # 4 个 Watcher 启动
  │   ├─ OCRTextWatcher   (1.0s)
  │   ├─ UIATreeWatcher   (0.5s)
  │   ├─ WindowWatcher    (零轮询)
  │   └─ TimerWatcher     (按需)
  └─ risk_detector.check()       # 风控检查
```

### 8.2 单次发布数据流

```
publish(task)
  ├─ _pre_check()                 # 登录 + 风控 + 弹窗 + 窗口
  ├─ _step_enter_moments()        # click + wait_for(TEXT_APPEARED)
  ├─ _step_type_text()            # type + wait_for(TIMER_EXPIRED)
  ├─ _step_add_images()           # paste + OCR 检测上传
  └─ _step_publish()              # click + wait_any(TEXT_APPEARED, TIMER_EXPIRED)
```

### 8.3 状态机转换

```
IDLE → ENTERING_MOMENTS → TYPING_CONTENT → [ADDING_IMAGES] → CONFIRMING_PUBLISH → DONE
                     ↑ 任一状态失败 3 次 → ERROR
                     ↑ 风控信号检测 → WAITING（冷却后恢复）
```

---

## 9. 关键技术决策

### 9.1 为什么 Python + C# 混合

| 考虑因素 | Python | C# |
|----------|--------|-----|
| OCR/CV 生态 | PaddleOCR, OpenCV 原生 | 需 ONNX 转换 | → Python |
| UIAutomation | pywin32 封装残缺 | FlaUI 一等公民 | → C# |
| Windows API | ctypes 啰嗦易错 | P/Invoke 直接 | → C# |
| 微信 4.x 无障碍触发 | ctypes 调 COM | 原生 `AutomationElement.FromHandle` | → C# |
| 开发速度 | 极快 | 中等 | → Python |

**结论**: Python 做 AI/编排/API 层，C# 做 Windows 原生交互层。

### 9.2 为什么事件驱动

| 维度 | 轮询 | 事件驱动 |
|------|------|----------|
| CPU 占用 | 持续消耗（循环扫描） | 近乎为零（等待唤醒） |
| 响应时间 | 0.5~3s（取决于轮询间隔） | ~100ms（事件发生即响应） |
| 代码耦合 | 高（Publisher 直接调用 Watcher） | 低（都通过 EventBus 通信） |
| 可扩展性 | 加功能 = 加扫描 = 加 CPU | 加功能 = 加订阅 = 零增量成本 |

### 9.3 为什么不锁定微信版本

| 可依赖（语义层，版本无关） | 不可依赖（表现层，随版本变化） |
|---------------------------|-------------------------------|
| 文字的语义内容 | 文字的精确像素值 |
| 图标的形状特征 (SIFT 描述符) | 图标的精确像素 |
| 元素的相对空间关系 | 元素的精确坐标 |
| 窗口类名 (WeChatMainWndForPC) | 控件 AutomationId |

---

## 10. 环境搭建与编译

### 10.1 Python 环境

```bash
git clone https://github.com/sebastEXlabe/wechat-moments-automation.git
cd wechat-moments-automation

python -m venv venv
venv\Scripts\activate  # Windows

pip install -r requirements.txt
python -c "from src.core import EventBus; print('OK')"
```

### 10.2 C# 微服务编译

```bash
# 需要 .NET 8.0 SDK: https://dotnet.microsoft.com/download
cd src/cs_uia_service
dotnet restore
dotnet publish -c Release -o publish
publish/WeChatUIA.exe dump-tree  # 验证
```

### 10.3 图标模板准备

```bash
# 方式 1: 从微信程序文件自动提取
python -c "
from src.locator.resource_extractor import ResourceCollector
c = ResourceCollector(r'C:\Program Files\Tencent\WeChat', 'templates/icons')
c.collect_all()
"

# 方式 2: 运行时自动截取
python -c "
from src.locator.template_extractor import update_all_templates
from src.locator.ocr_locator import OCRLocator
update_all_templates(OCRLocator())
"
```

### 10.4 启动 API Server

```bash
python -m uvicorn src.api.server:app --host 127.0.0.1 --port 18080
```

### 10.5 启动 Electron 前端

```bash
# 先完成 sparkle-ref 的 3 处修改（见第 7.3 节）
cd C:\Users\woshi\Downloads\sparkle-ref
pnpm dev
```

### 10.6 运行

```bash
# 交互模式
python main.py --interactive

# 单次发布
python main.py --text "今天天气真好"

# 带图片
python main.py --text "分享照片" --images photo1.jpg photo2.jpg

# 批量发布
python main.py --batch posts.txt
```

---

## 11. 配置文件说明

`config/settings.yaml` 全部参数：

```yaml
ocr:
  engine: "paddleocr"          # paddleocr | easyocr
  paddleocr:
    lang: "ch"
    det_db_thresh: 0.3          # 检测阈值 (越低越敏感)
  cache_ttl: 2.0                # OCR 缓存有效期 (秒)

feature_matching:
  algorithm: "orb"              # orb | sift
  orb_features: 2000            # ORB 特征点数量
  lowe_ratio: 0.65              # Lowe's ratio test (越低越严格)
  min_good_matches: 10          # 最少优质匹配数
  ransac_threshold: 5.0         # RANSAC 重投影误差阈值

human_simulation:
  base_delay: 3.0               # 基础操作延迟 (秒)
  delay_shape: 3.0              # Gamma shape (越大越集中)
  click_jitter: 3               # 点击位置随机抖动 (px)
  extra_action_probability: 0.3 # 多余动作概率 (0-1)
  typing_wpm_range: [60, 100]   # 打字速度范围

moments:
  labels:                       # 文字标签 (版本无关)
    nav_moments: "朋友圈"
    btn_publish: "发表"
    msg_success: "已发送"
  publish_verify_timeout: 10.0  # 发布验证超时 (秒)

safety:
  daily_limits:
    max_posts: 10               # 每日最多发圈
    max_likes: 50               # 每日最多点赞
    max_comments: 20            # 每日最多评论
  task_interval_range: [30, 120] # 任务间间隔 (秒)
  cooldown_base_minutes: 2      # 风控冷却基数
  max_cooldown_seconds: 21600   # 最大冷却时间 (6小时)

logging:
  level: "INFO"
  rotation: "10 MB"
  retention: "7 days"
  path: "logs/automation_{time}.log"
```

---

## 12. 扩展指南

### 12.1 添加新的定位策略

```python
class MyLocator:
    def locate(self, target) -> Optional[Tuple[int, int]]:
        pass  # 你的定位逻辑

# 注册到路由器
class ExtendedRouter(LocateRouter):
    def locate(self, element):
        result = self.my_locator.locate(element)
        if result:
            return result
        return super().locate(element)
```

### 12.2 添加新的 Watcher

```python
from src.core.watchers import BaseWatcher

class MyWatcher(BaseWatcher):
    def __init__(self, bus, interval=1.0):
        super().__init__(bus, "MyWatcher", interval)

    def _run_loop(self):
        while self._running:
            if detected_something():
                self.bus.emit(Event(EventType.MY_EVENT, self.name, {...}))
            time.sleep(self.interval)

# 注册到 WatchManager
manager.my_watcher = MyWatcher(bus)
manager.my_watcher.start()
```

### 12.3 添加新的 API 端点

```python
# 在 src/api/server.py 中添加
@app.post("/api/my-new-endpoint")
async def my_new_endpoint(req: MyModel):
    result = do_something(req)
    return {"success": True, "data": result}
```

### 12.4 添加新的朋友圈功能

```python
# 在 MOMENTS_ELEMENTS 中添加新元素
MOMENTS_ELEMENTS['btn_location'] = ElementDescriptor(
    name="所在位置",
    ocr_text="所在位置",
)

# 在 publisher 中添加新的处理步骤
def _step_set_location(self, location_name: str) -> bool:
    self.operator.click_element(MOMENTS_ELEMENTS['btn_location'])
    event = self.bus.wait_for(EventType.TEXT_APPEARED,
                              payload_match={'matched': location_name},
                              timeout=5.0)
    if event:
        pyautogui.click(event.payload['x'], event.payload['y'])
        return True
    return False
```

### 12.5 自定义类人行为参数

```python
from src.executor.human_sim import HumanSimulator, SimulationConfig

config = SimulationConfig(
    base_delay=5.0,                      # 更慢的节奏
    extra_action_probability=0.5,        # 更多多余动作
    typing_wpm_range=(40, 60),           # 更慢的打字
    bezier_offset_range=(-120, 120),     # 更弯的鼠标轨迹
)
sim = HumanSimulator(config)
```

---

## 13. 故障排查

### 13.1 微信窗口未找到

```
错误: "微信窗口未找到，请确认微信已启动"
排查:
  1. 确认微信已启动且可见（非最小化）
  2. 确认窗口类名: win32gui.FindWindow("WeChatMainWndForPC", None)
  3. 微信 4.x 如果改变了类名，更新 config/settings.yaml
```

### 13.2 C# UIA 服务不可用

```
错误: "WeChatUIA.exe 未找到"
排查:
  1. cd src/cs_uia_service && dotnet publish -c Release -o publish
  2. dotnet --version  # 确认 .NET 8.0+
  3. 系统自动回退到纯 OCR 模式，功能不受影响但性能略降
```

### 13.3 OCR 安装问题

```bash
# PaddleOCR 安装
pip install paddlepaddle
pip install paddleocr

# 替代方案
pip install easyocr
# 修改 config/settings.yaml: ocr.engine = "easyocr"
```

### 13.4 OpenCV contrib 问题

```bash
# 仅使用 SIFT 时需要（默认 ORB 不需要）
pip uninstall opencv-python
pip install opencv-contrib-python
```

### 13.5 API Server 无法连接

```
错误: "API 不可用" / "连接拒绝"
排查:
  1. 确认 Python API Server 已启动
  2. 检查端口: curl http://127.0.0.1:18080/health
  3. 检查防火墙是否拦截
```

### 13.6 前端页面空白

```
错误: Electron 中 /wechat 页面空白
排查:
  1. 确认已在 routes/index.tsx 添加路由
  2. 确认已在 App.tsx 注册 componentMap + siderCardRouteMap
  3. 打开 DevTools → Console 查看错误
  4. 确认 API Server 在 localhost:18080 运行
```

### 13.7 调试截图

```
logs/failures/                     — 定位失败自动截图
debug_screenshots/                 — 手动调试截图

分析定位失败:
  1. 打开 PNG 查看当前屏幕状态
  2. 确认目标元素是否确实存在
  3. 存在但定位失败 → 调整 confidence 阈值
  4. 不存在 → 检查微信状态（掉线/弹窗/页面变化）
```

### 13.8 日志分析

```bash
# 启用 DEBUG 日志
# config/settings.yaml: logging.level = "DEBUG"

# 关键日志模式:
"OCR 扫描完成: N 个文本块"          — OCR 正常
"文本块数量异常少 (<5)"             — 窗口可能最小化
"等待 '[文字]' 超时"                — 页面未加载或不存在
"定位失败截图已保存"                — 查看 logs/failures/
"风控信号: operation_too_frequent"  — 被风控，等待冷却
"检测到窗口移动"                    — 自动校准已触发
```

# 开发文档

## 目录

1. [设计哲学](#1-设计哲学)
2. [架构全景图](#2-架构全景图)
3. [事件驱动机制](#3-事件驱动机制)
4. [模块详解](#4-模块详解)
5. [数据流与状态转换](#5-数据流与状态转换)
6. [关键技术决策](#6-关键技术决策)
7. [环境搭建与编译](#7-环境搭建与编译)
8. [配置文件说明](#8-配置文件说明)
9. [扩展指南](#9-扩展指南)
10. [故障排查](#10-故障排查)

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

### 1.2 根本设计思路

传统 GUI 自动化的核心问题是**版本相关的定位依赖**：

```
旧范式：找"特定版本的特定像素/控件属性" → 版本一变全崩
新范式：找"不随版本变化的语义特征"     → 版本更新仅需微调
```

三个语义维度对应三种定位策略：

| 语义维度 | 定位策略 | 覆盖场景 |
|----------|----------|----------|
| **文字语义** | OCR 找文字标签（"朋友圈"永远是"朋友圈"） | 80% 带文字的按钮/标签 |
| **图形语义** | SIFT/ORB 特征点匹配图标形状 | 15% 纯图标的按钮 |
| **空间语义** | 多锚点相对定位推断未知元素 | 5% 输入框等无标签元素 |

### 1.3 事件驱动 vs 传统轮询

```
轮询驱动的浪费：
  click("朋友圈") → sleep(1.5s) → OCR扫描 → sleep(0.5s) → OCR扫描 → 找到了
      点击               等待          扫            等           扫          继续
  时间线：[====操作系统已在0.8s完成加载，但我们还在sleep等轮询间隔====]

事件驱动的精准：
  click("朋友圈") → wait_for(text.appeared("这一刻的想法")) → 继续
      点击               系统在后台等，事件0.8s到达即响应
  时间线：   ✓ 零CPU空转  ✓ 响应时间=事件发生时间
```

---

## 2. 架构全景图

### 2.1 五层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         业务层                                  │
│    EventDrivenPublisher (src/core/publisher.py)                 │
│    朋友圈发布流程编排（事件驱动状态机）                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │   emit(events) / wait_for(events)
┌───────────────────────────▼─────────────────────────────────────┐
│                      事件驱动核心层                             │
│    EventBus (src/core/events.py)                                │
│    ├─ 发布/订阅 + 通配符 + once() + wait_for() + wait_any()     │
│    └─ 历史记录 + 线程安全                                       │
│                                                                 │
│    WatchManager (src/core/watchers.py)                          │
│    ├─ OCRTextWatcher  ─ 监测屏幕上文字的出现/消失               │
│    ├─ UIATreeWatcher  ─ 监测 UIA 控件树变化                     │
│    ├─ WindowWatcher   ─ 监测窗口位置/大小变化 (零轮询)           │
│    └─ TimerWatcher    ─ 事件驱动的定时器                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
┌───────▼──────┐   ┌────────▼───────┐   ┌───────▼──────┐
│   定位层      │   │    执行层       │   │   监控层      │
│ (locator/)   │   │  (executor/)   │   │  (monitor/)  │
│              │   │                │   │              │
│ OCR 定位     │   │ 类人延迟(Gamma) │   │ 风控信号检测  │
│ SIFT/ORB匹配  │   │ 贝塞尔鼠标轨迹 │   │ 弹窗自动清理  │
│ 锚点相对定位  │   │ 键盘时序模拟   │   │ 指数退避冷却  │
│ 策略路由      │   │ 文件对话框     │   │              │
│ PE 资源提取   │   │ C# UIA 桥接    │   │              │
│ Protobuf OCR  │   │                │   │              │
└───────┬──────┘   └────────┬───────┘   └───────┬──────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                      恢复层                                     │
│    ErrorRecovery (src/recovery/)                                │
│    多层级异常恢复（定位降级 → 窗口激活 → 进程重启）             │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 进程架构

```
┌─────────────────┐     JSON/stdout     ┌──────────────────┐
│   Python 主控    │◄───────────────────►│  C# UIA 微服务    │
│                  │   subprocess 调用   │  (WeChatUIA.exe)  │
│  · 流程编排      │                    │                  │
│  · OCR/CV 处理   │                    │  · 控件树 dump    │
│  · 类人模拟      │                    │  · 窗口监控       │
│  · 风控策略      │                    │  · 无障碍触发     │
│  · 事件总线      │                    │  · 登录状态检测   │
└─────────────────┘                    └──────────────────┘
         │                                      │
         │         操作系统公开 API              │
         └──────────────┬──────────────────────-┘
                        │
              ┌─────────▼─────────┐
              │   微信客户端        │
              │  (WeChat.exe)      │
              └───────────────────┘
```

---

## 3. 事件驱动机制

### 3.1 EventBus 设计

事件总线是整个系统的神经中枢。所有组件通过它通信，而不是直接相互调用。

```python
from src.core.events import EventBus, Event, EventType

bus = EventBus()

# ── 订阅 ──
bus.on(EventType.TEXT_APPEARED, lambda e: print(f"文字出现: {e}"))

# ── 一次性订阅（触发后自动取消） ──
bus.once(EventType.WINDOW_MOVED, lambda e: recalibrate())

# ── 发布 ──
bus.emit(Event(EventType.TEXT_APPEARED, "OCRWatcher", {
    'text': '已发送', 'x': 500, 'y': 600
}))

# ── 等待事件（事件驱动替代 time.sleep） ──
event = bus.wait_for(EventType.TEXT_APPEARED,
                     payload_match={'text': '已发送'},
                     timeout=10.0)
if event:
    print("发布成功")  # 事件到达即返回，无轮询
else:
    print("超时")

# ── 等待任意事件 ──
event = bus.wait_any([EventType.TEXT_APPEARED, EventType.TIMER_EXPIRED],
                     timeout=15.0)
```

### 3.2 事件类型全集

| 事件类型 | 触发源 | 携带数据 |
|----------|--------|----------|
| `element.appeared` | UIA Watcher | name, x, y, controlType |
| `element.vanished` | UIA Watcher | name |
| `text.appeared` | OCR Watcher | text, matched, x, y, confidence |
| `text.vanished` | OCR Watcher | text, matched |
| `window.moved` | WindowWatcher (WinEventHook) | left, top, dx, dy |
| `window.resized` | WindowWatcher | width, height, dw, dh |
| `window.minimized` | WindowWatcher | — |
| `login.lost` | OCR Watcher | detectedPage |
| `risk.warning` | RiskDetector | cooldown, signal |
| `risk.critical` | RiskDetector | signal |
| `popup.detected` | PopupHandler | name, type |
| `step.started` / `step.completed` / `step.failed` | Publisher | step, elapsed |
| `publish.confirmed` | Publisher | — |
| `upload.complete` | OCR Watcher | — |
| `timer.expired` | TimerWatcher | reason |

### 3.3 事件驱动的发布流程

```
IDLE
  │  publisher.emit(STEP_STARTED)
  ▼
ENTERING_MOMENTS
  │  operator.click("朋友圈")
  │       │
  │       ▼
  │  wait_for(TEXT_APPEARED, matched="这一刻的想法", timeout=5s)
  │       │
  │       ├─ 事件到达 → ✅ 进入下一步
  │       └─ 超时     → 清理弹窗 → 重试 (最多 3 次)
  ▼
TYPING_CONTENT
  │  operator.type(text)
  │  wait_for(TIMER_EXPIRED, reason="typing_complete", timeout=2s)
  ▼
ADDING_IMAGES
  │  粘贴图片 → wait_for(TIMER_EXPIRED) → OCR 检测上传状态
  │       │
  │       ├─ 无"上传失败"文字 → ✅ 完成
  │       └─ 有"上传失败"     → 重试
  ▼
CONFIRMING_PUBLISH
  │  operator.click("发表")
  │  wait_any([TEXT_APPEARED, TIMER_EXPIRED], timeout=15s)
  │       │
  │       ├─ "已发送" appeared → ✅ DONE
  │       └─ 超时/"失败"      → 重试 (最多 3 次)
  ▼
DONE
```

### 3.4 Watcher 的事件产生机制

```
OCRTextWatcher (每 1.0s):
  OCR扫描 → 文字集合 {A, B, C}
  对比上一帧 {A, B}
  新出现 C → emit(TEXT_APPEARED, {text: C, ...})

UIATreeWatcher (每 0.5s):
  UIA dump → 元素名称集合
  对比上一帧 → diff → emit(ELEMENT_APPEARED / ELEMENT_VANISHED)

WindowWatcher (零轮询):
  SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE) 注册操作系统回调
  窗口移动 → OS 通知 → emit(WINDOW_MOVED, {dx, dy})

TimerWatcher (按需):
  threading.Timer(seconds, callback) → 到期 → emit(TIMER_EXPIRED)
```

---

## 4. 模块详解

### 4.1 定位层 (src/locator/)

#### 4.1.1 OCR 文字定位器 (`ocr_locator.py`)

这是系统的**主力定位手段**（覆盖 80% 场景）。

```
输入: 文字标签 "朋友圈"
输出: TextBlock(x=192, y=28, confidence=0.97)
原理: PaddleOCR / 微信原生 OCR / EasyOCR
特点: 版本无关、DPI 无关、主题无关
```

**接口**:
- `find_text(target) → List[TextBlock]` — 搜索包含目标文字的文本块
- `find_best(target) → Optional[TextBlock]` — 置信度最高的匹配
- `click_text(target) → bool` — 查找并点击
- `wait_text(target, timeout) → Optional[TextBlock]` — 等文字出现
- `scan_screen(region=None) → List[TextBlock]` — 全屏 OCR
- `dump_screen_text() → str` — 调试用，打印所有识别到的文字

**引擎选择逻辑**:
```
UnifiedOCREngine:
  1. 尝试微信原生 OCR (WeChatOCR.exe + Mojo IPC)
  2. 不可用 → PaddleOCR
  3. 不可用 → EasyOCR
```

#### 4.1.2 特征点匹配定位器 (`feature_locator.py`)

覆盖 OCR 找不到的**纯图标按钮**。

```
输入: 图标模板 "camera_icon.png"
输出: (center_x, center_y) 或 None
原理: ORB/SIFT 特征点提取 → knnMatch → Lowe's ratio test → RANSAC 单应性
特点: 缩放不变、旋转不变、光照不敏感
```

| 算法 | 精度 | 速度 | 专利 |
|------|------|------|------|
| SIFT | 最高 | 200-500ms | 有专利（需 opencv-contrib） |
| ORB（默认） | 高 | 50-200ms | 无（开源） |

#### 4.1.3 多锚点校准器 (`anchor_locator.py`)

运行时自动校准，建立当前版本的坐标映射。

```
启动时:
  1. detect_window()           → 获取微信窗口尺寸
  2. scan_navigation()         → OCR 扫导航栏 → 建立 nav_* 锚点
  3. locate_icons()            → SIFT 特征匹配定位图标 → icon_* 锚点
  4. enter_moments_and_scan()  → 进入朋友圈页面 → moments_* 锚点
  5. build_mapping()           → 输出 CoordinateMapping

运行时:
  locate_relative("nav_朋友圈", dx=30, dy=180)  → 推算目标位置
```

#### 4.1.4 策略路由器 (`router.py`)

**定位优先级**:
```
find_element(target):
  策略 1: OCR 文字定位     → fastest, most reliable
  策略 2: SIFT/ORB 特征匹配 → for icon-only elements
  策略 3: 锚点相对定位     → inference from known positions
  策略 4: 像素模板匹配     → last resort (version-sensitive)
  全失败  → 截图 + 日志    → 调试用
```

**预定义元素库** (`MOMENTS_ELEMENTS`):
- `nav_moments` — "朋友圈"导航
- `input_hint` — "这一刻的想法"输入框
- `btn_add_photo` — "相册"按钮
- `btn_publish` — "发表"按钮
- `msg_success` — "已发送"成功提示

#### 4.1.5 PE 资源提取器 (`resource_extractor.py`)

```
策略 A（首选）: pefile 解析 WeChatWin.dll .rsrc 段
  → 提取 ICON / BITMAP / PNG / RCData
  → 自动转换为 PNG

策略 B（备选）: GDI/GDI+ Hook 注入
  → Hook GdipLoadImageFromFile / LoadImageW
  → 拦截微信加载的每一个图像
  → 自动保存
```

#### 4.1.6 运行时模板提取器 (`template_extractor.py`)

```
输入: 文字标签列表 ["朋友圈", "发表", "相册"]
流程:
  1. OCR 找到文字位置
  2. 截图文字周围区域
  3. Canny 边缘检测 → 轮廓提取 → 精确裁剪按钮边界
  4. 保存为 PNG 模板
```

#### 4.1.7 微信原生 OCR (`wechat_native_ocr.py`)

**Protobuf schema** (内嵌):
```protobuf
message OcrRequest {
    optional bytes image_data = 1;
    optional int32 task_id = 2;
}

message OcrResponse {
    optional int32 task_id = 1;
    optional int32 err_code = 2;
    optional OcrResult result = 4;
}

message OcrResult {
    repeated LineResult lines = 1;  // 每行结果
}

message LineResult {
    repeated CharResult chars = 1;  // 每字符
    optional Box box = 2;           // 包围框
    optional string text = 3;       // 完整文字
}
```

**通信协议**: Google Protocol Buffers + Chromium Mojo IPC

### 4.2 执行层 (src/executor/)

#### 4.2.1 类人行为模拟 (`human_sim.py`)

| 行为 | 算法 | 参数 |
|------|------|------|
| 操作延迟 | `Gamma(shape=3, scale=base/3) + uniform(0.7, 1.3)` | 5% 概率"走神" ×1.5~3 |
| 鼠标轨迹 | 二次贝塞尔 `(1-t)²P0 + 2(1-t)tP1 + t²P2` | ±80px 控制点随机偏移, ±2px 每步抖动 |
| 键盘输入 | 逐字符 `60/WPM` 为基础延迟, 高频字 0.3~0.6×, 标点 2.5~4.5× | WPM=60-100 |
| 多余操作 | 30% 概率: 晃鼠标 / 看向别处 / 犹豫 / 滚轮 | — |

**贝塞尔曲线速度曲线**:
```
t < 0.2  → 加速阶段（稍慢，从静止启动）
0.2~0.8 → 匀速阶段（最快）
t > 0.8 → 减速阶段（逼近目标，精准调整）
```

#### 4.2.2 C# UIA 桥接 (`uia_bridge.py`)

Python 与 C# 微服务的通信层。

```python
bridge = UIABridge()

# 控件树操作
tree = bridge.dump_tree()              # JSON 格式完整控件树
buttons = bridge.get_all_clickable()    # 所有可点击按钮
elem = bridge.find_elements_by_name("发表")  # 按名称查找

# 窗口操作
bridge.activate_window()               # 激活/置顶微信窗口
rect = bridge.get_window_rect()        # 获取窗口位置

# 登录检测
result = bridge.check_login()          # 是否已登录
# → {'isLoggedIn': True, 'detectedPage': '微信主界面', ...}

# 窗口监控（零轮询 WinEventHook）
bridge.start_window_monitor(on_change=callback)
```

**C# 服务编译**:
```bash
cd src/cs_uia_service
dotnet publish -c Release -o publish
# 输出: publish/WeChatUIA.exe
```

#### 4.2.3 文件对话框 (`file_dialog.py`)

**策略优先级**:
```
策略 A: CF_DIB 剪贴板图片粘贴 → Ctrl+V
  微信朋友圈原生支持，完全绕过文件对话框

策略 A2: CF_HDROP 文件路径粘贴 → Ctrl+V
  直接将文件路径写入剪贴板，粘贴即上传

策略 B: pywinauto → dlg.Edit.set_text(path) → dlg["打开"].click()
  操控 Windows 标准文件对话框

策略 C: pynput → 键盘模拟输入路径 → Enter
  最后兜底方案
```

#### 4.2.4 操作执行器 (`operator.py`)

**升级后的核心能力**:
- `click_by_uia(name)` — 通过 UIA 控件树直接点击（100% 准确）
- `check_login_state()` — 检测微信登录状态
- `start_window_monitoring(on_recalibrate)` — 启动窗口监控
- `_wait_image_upload(timeout)` — 等待图片上传完成

### 4.3 监控层 (src/monitor/)

#### 4.3.1 风控检测器 (`risk_detector.py`)

**检测信号** (版本无关的文字匹配):
| 信号 | 等级 | 处理 |
|------|------|------|
| "重新登录" | CRITICAL | 停止所有操作 |
| "账号已被限制" | DANGER | 停止 + 长时间冷却 |
| "操作太频繁" | WARNING | 指数退避冷却 |
| "安全验证" | DANGER | 停止 + 需要人工 |
| "版本过低" | CRITICAL | 需要升级微信 |

**指数退避冷却**:
```
第 1 次 → 2 分钟
第 2 次 → 4 分钟
第 3 次 → 8 分钟
第 4 次 → 16 分钟
达到上限 → 停止当天所有操作
```

#### 4.3.2 弹窗处理器 (`popup_handler.py`)

**弹窗优先级**:
```
BLOCKING: 版本更新、强制登出 → 立即处理
HIGH:     安全验证 → 暂停操作
NORMAL:   消息通知 → 延后处理
LOW:      非阻断通知 → 可忽略
```

### 4.4 C# UIA 微服务 (src/cs_uia_service/)

#### 4.4.1 技术栈

- **FlaUI.UIA3**: Windows UIAutomation 最佳 .NET 封装
- **System.Text.Json**: JSON 序列化
- **user32.dll P/Invoke**: FindWindow, SetForegroundWindow, SetWinEventHook, GetWindowRect

#### 4.4.2 命令接口

| 命令 | 输出 | 用途 |
|------|------|------|
| `dump-tree` | 完整控件树 JSON | Python 获取 UI 结构 |
| `activate` | `{"success": true}` | 激活微信窗口 |
| `monitor` | 持续输出窗口信息 JSON | 窗口位置监控 |
| `check-login` | 登录状态 JSON | 掉线检测 |
| `get-window-rect` | 窗口矩形 JSON | 获取位置尺寸 |

#### 4.4.3 控件树 JSON 格式

```json
{
  "window": {
    "left": 0, "top": 0, "right": 1200, "bottom": 800,
    "title": "微信", "className": "WeChatMainWndForPC"
  },
  "rootElement": {
    "controlType": "Window",
    "name": "微信",
    "automationId": "",
    "children": [
      {
        "controlType": "Button",
        "name": "朋友圈",
        "automationId": "momentsBtn",
        "x": 192, "y": 0,
        "width": 64, "height": 56,
        "isEnabled": true,
        "children": []
      }
    ]
  },
  "totalElements": 156,
  "timestamp": 1718123456789
}
```

#### 4.4.4 关键代码解析

**附着窗口 + 触发无障碍模式**:
```csharp
// 找到微信进程
Process[] processes = Process.GetProcessesByName("WeChat");
var app = FlaUI.Core.Application.Attach(processes[0].Id);

// 附着主窗口 —— 这一步触发微信的"无障碍模式"
// 微信检测到 UIA Client 接入后，自动暴露完整控件树
var window = app.GetMainWindow(new UIA3Automation());

// 使用 ControlViewWalker 遍历（过滤纯布局元素）
var walker = automation.TreeWalkerFactory.GetControlViewWalker();
var child = walker.GetFirstChild(window);
```

**WinEventHook 零轮询窗口监控**:
```csharp
// 在操作系统层面注册窗口变化回调
// 窗口移动 → OS 通知 → 回调执行 → 0ms 延迟
var hook = SetWinEventHook(
    EVENT_OBJECT_LOCATIONCHANGE,  // 监听位置变化
    EVENT_OBJECT_LOCATIONCHANGE,
    IntPtr.Zero,
    (hHook, eventType, hWnd, ...) => {
        if (hWnd == wechatWindowHandle) {
            // 输出 JSON 到 stdout
            Console.WriteLine(JsonSerializer.Serialize(windowInfo));
        }
    },
    0, 0, WINEVENT_OUTOFCONTEXT
);
```

---

## 5. 数据流与状态转换

### 5.1 初始化数据流

```
main.py
  │
  ▼
publisher.initialize()
  │
  ├─ 1. operator.find_wechat_window()
  │     └─ win32gui.FindWindow("WeChatMainWndForPC")
  │
  ├─ 2. operator.ensure_window_active()
  │     └─ SetForegroundWindow + 置顶 + 取消最小化
  │
  ├─ 3. operator.check_login_state()
  │     ├─ 优先: C# UIA dump-tree → 检查 nav 标签
  │     └─ 回退: OCR 扫描 → 查找 "聊天""通讯录"
  │
  ├─ 4. calibrator.calibrate()
  │     ├─ OCR 扫描导航栏 → nav_* 锚点
  │     ├─ SIFT 特征匹配 → icon_* 锚点
  │     └─ 进入朋友圈扫描 → moments_* 锚点
  │
  ├─ 5. WatchManager.start_all()
  │     ├─ OCRTextWatcher   (1.0s 间隔)
  │     ├─ UIATreeWatcher   (0.5s 间隔)
  │     ├─ WindowWatcher    (WinEventHook 零轮询)
  │     └─ TimerWatcher     (按需)
  │
  └─ 6. risk_detector.check(force=True)
        └─ OCR 扫描风控关键词
```

### 5.2 单次发布数据流

```
publisher.publish(task)
  │
  ├─ _pre_check()
  │   ├─ check_login_state()     ← 事件源: UIA
  │   ├─ risk_detector           ← 事件源: OCR
  │   └─ popup_handler           ← 事件源: OCR
  │
  ├─ _step_enter_moments()
  │   ├─ click_by_uia("朋友圈")   ← 走 C# UIA
  │   ├─ wait_for(TEXT_APPEARED) ← 事件驱动等待
  │   └─ 超时 → popup_handler → retry
  │
  ├─ _step_type_text()
  │   ├─ click_element(input_hint) ← OCR 定位
  │   ├─ human_sim.type_text()     ← 类人输入
  │   └─ wait_for(TIMER_EXPIRED)   ← 事件确认
  │
  ├─ _step_add_images()
  │   ├─ file_dialog.paste()       ← CF_DIB 剪贴板
  │   ├─ OCR 检测上传状态          ← 轮询（OCR 无法检测上传进度）
  │   └─ 失败 → retry
  │
  └─ _step_publish()
      ├─ click_element("发表")     ← OCR 定位
      ├─ wait_any([TEXT_APPEARED, TIMER_EXPIRED])
      └─ "发送失败" → retry
```

### 5.3 状态机转换图

```
                    ┌─────────────────────────────┐
                    │           IDLE               │
                    └──────────────┬──────────────┘
                                   │ STEP_STARTED
                    ┌──────────────▼──────────────┐
                    │     ENTERING_MOMENTS         │
                    │  等待 text.appeared(        │
                    │  "这一刻的想法")             │
                    └──────────────┬──────────────┘
                                   │ 事件到达
                    ┌──────────────▼──────────────┐
                    │      TYPING_CONTENT          │
                    │  human_sim.type_text()      │
                    │  等待 timer.expired          │
                    └──────────────┬──────────────┘
                                   │
                          ┌────────┴────────┐
                          │  有图片?         │
                          └────────┬────────┘
                    ┌──────────────▼──────┐  ┌──▼─────────────────┐
                    │   ADDING_IMAGES     │  │  (skip)            │
                    │ paste + 等上传完成  │  └──────────┬─────────┘
                    └──────────────┬──────┘             │
                                   └─────────┬──────────┘
                                             │
                    ┌────────────────────────▼─────────┐
                    │       CONFIRMING_PUBLISH          │
                    │  click("发表")                    │
                    │  等待 text.appeared("已发送")     │
                    └──────────────┬───────────────────┘
                                   │ "已发送" appeared
                    ┌──────────────▼───────────────────┐
                    │            DONE                  │
                    │  记录统计 + 事件通知              │
                    └──────────────────────────────────┘

     任一状态失败 3 次:
          │
          ▼
     ┌──────────┐
     │  ERROR   │ → 记录截图 + 日志 + 通知
     └──────────┘

     风控信号检测:
          │
          ▼
     ┌──────────┐
     │ WAITING  │ → 指数退避冷却 → 恢复到之前状态
     └──────────┘
```

---

## 6. 关键技术决策

### 6.1 为什么选择 Python + C# 混合架构

| 考虑因素 | Python | C# | 结论 |
|----------|--------|----|----|
| OCR/CV 生态 | PaddleOCR, OpenCV 原生支持 | 需 ONNX 转换或第三方封装 | Python |
| UIAutomation | pywin32 封装残缺 | FlaUI 一等公民 | C# |
| Windows API | ctypes 调用啰嗦 | P/Invoke 直接 | C# |
| 开发速度 | 极快 | 中等 | Python |
| 微信 4.x 无障碍触发 | ctypes 调 COM | 原生 `AutomationElement.FromHandle` | C# |
| 部署 | 需 Python 运行时 | 单文件 exe | 各自优势 |

**结论**: Python 做 AI/编排层，C# 做 Windows 原生交互层。

### 6.2 为什么事件驱动优于轮询

| 维度 | 轮询 | 事件驱动 |
|------|------|----------|
| CPU 占用 | 持续消耗（循环扫描） | 近乎为零（等待唤醒） |
| 响应时间 | 0.5~3s（取决于轮询间隔） | ~100ms（事件发生即响应） |
| 代码耦合 | 高（Publisher 直接调用 Watcher 方法） | 低（都通过 EventBus 通信） |
| 可扩展性 | 加功能 = 加扫描 = 加 CPU | 加功能 = 加订阅 = 零增量成本 |
| 异常处理 | 在扫描循环里 try/catch | 事件处理器独立 try/catch |

### 6.3 为什么不锁定微信版本

用户明确要求不锁定版本。这意味着不能依赖任何可能随版本变化的属性：

| 可依赖（语义层） | 不可依赖（表现层） |
|-----------------|-------------------|
| 文字的语义内容 ("朋友圈" 永远叫 "朋友圈") | 文字的像素表现 (字体、颜色、大小) |
| 图标的形状特征 (SIFT 描述符对缩放/光照不变) | 图标的精确像素值 |
| 元素的相对空间关系 | 元素的精确坐标 |
| 窗口类名 (WeChatMainWndForPC 是公开 API) | 控件 AutomationId (随版本变化) |

---

## 7. 环境搭建与编译

### 7.1 Python 环境

```bash
# 要求 Python 3.10+

# 克隆仓库
git clone https://github.com/sebastEXlabe/wechat-moments-automation.git
cd wechat-moments-automation

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

# 安装依赖
pip install -r requirements.txt

# 验证安装
python -c "from src.core import EventBus; print('OK')"
```

### 7.2 C# 微服务编译

```bash
# 要求 .NET 8.0 SDK
# 下载: https://dotnet.microsoft.com/download

cd src/cs_uia_service

# 恢复 NuGet 包
dotnet restore

# 编译并发布（单文件 exe）
dotnet publish -c Release -o publish

# 验证编译
publish/WeChatUIA.exe dump-tree
# 如果微信正在运行，应输出控件树 JSON
```

### 7.3 图标模板准备

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

### 7.4 运行

```bash
# 交互模式
python main.py --interactive

# 单次发布
python main.py --text "今天天气真好"

# 带图片
python main.py --text "分享照片" --images photo1.jpg photo2.jpg

# 批量发布
python main.py --batch posts.txt

# 空跑测试（不实际发布）
python main.py --text "测试" --dry-run
```

---

## 8. 配置文件说明

`config/settings.yaml` 中所有可调参数：

```yaml
# ── OCR 引擎 ──
ocr:
  engine: "paddleocr"          # paddleocr | easyocr
  paddleocr:
    lang: "ch"
    det_db_thresh: 0.3         # 检测阈值 (越低越敏感)
  cache_ttl: 2.0               # OCR 缓存有效期 (秒)

# ── 特征匹配 ──
feature_matching:
  algorithm: "orb"             # orb | sift
  orb_features: 2000           # ORB 特征点数量
  lowe_ratio: 0.65             # Lowe's ratio test (越低越严)
  min_good_matches: 10         # 最少优质匹配
  ransac_threshold: 5.0       # RANSAC 重投影误差

# ── 类人行为 ──
human_simulation:
  base_delay: 3.0              # 基础延迟 (秒)
  delay_shape: 3.0             # Gamma shape (越大越集中)
  click_jitter: 3              # 点击随机抖动 (px)
  extra_action_probability: 0.3 # 多余动作概率
  typing_wpm_range: [60, 100]  # 打字速度

# ── 朋友圈 ──
moments:
  labels:                      # 文字标签 (版本无关)
    nav_moments: "朋友圈"
    btn_publish: "发表"
    msg_success: "已发送"
  publish_verify_timeout: 10.0 # 发布验证超时

# ── 安全 ──
safety:
  daily_limits:
    max_posts: 10              # 每日最多发圈
    max_likes: 50              # 每日最多点赞
  task_interval_range: [30, 120] # 任务间间隔 (秒)
  cooldown_base_minutes: 2     # 冷却基数
  max_cooldown_seconds: 21600  # 最大冷却 (6h)

# ── 日志 ──
logging:
  level: "INFO"
  rotation: "10 MB"
  retention: "7 days"
```

---

## 9. 扩展指南

### 9.1 添加新的定位策略

```python
# 1. 实现你的定位器
class MyLocator:
    def locate(self, target) -> Optional[Tuple[int, int]]:
        # 你的定位逻辑
        pass

# 2. 注册到路由器
from src.locator.router import LocateRouter

class ExtendedRouter(LocateRouter):
    def locate(self, element):
        # 先试你的定位器
        result = self.my_locator.locate(element)
        if result:
            return result
        # 回退到默认策略
        return super().locate(element)
```

### 9.2 添加新的事件类型

```python
from src.core.events import EventType, Event

# 1. 在 EventType 枚举中添加
# 编辑 src/core/events.py:
#   MY_NEW_EVENT = "my.new.event"

# 2. 发布事件
bus.emit(Event(EventType.MY_NEW_EVENT, "my_component", {...}))

# 3. 订阅事件
bus.on(EventType.MY_NEW_EVENT, lambda e: handle(e))
```

### 9.3 添加新的 Watcher

```python
from src.core.watchers import BaseWatcher

class MyWatcher(BaseWatcher):
    def __init__(self, bus, interval=1.0):
        super().__init__(bus, "MyWatcher", interval)

    def _run_loop(self):
        while self._running:
            # 你的监测逻辑
            if detected_something():
                self.bus.emit(Event(...))
            time.sleep(self.interval)

# 注册到 WatchManager
manager = WatchManager(bus)
manager.my_watcher = MyWatcher(bus)
manager.my_watcher.start()
```

### 9.4 添加新的朋友圈功能

```python
# 在 MOMENTS_ELEMENTS 中添加新元素描述
MOMENTS_ELEMENTS['btn_location'] = ElementDescriptor(
    name="所在位置",
    ocr_text="所在位置",
    anchor_ref=('moments_这一刻的想法', 0, 60),
)

# 在 publisher 中添加新的处理步骤
def _step_set_location(self, location_name: str) -> bool:
    self.operator.click_element(MOMENTS_ELEMENTS['btn_location'])
    # 等待位置选择页面加载
    event = self.bus.wait_for(EventType.TEXT_APPEARED,
                              payload_match={'matched': location_name},
                              timeout=5.0)
    if event:
        pyautogui.click(event.payload['x'], event.payload['y'])
        return True
    return False
```

### 9.5 自定义类人行为参数

```python
from src.executor.human_sim import HumanSimulator, SimulationConfig

config = SimulationConfig(
    base_delay=5.0,                     # 更慢的操作节奏
    extra_action_probability=0.5,       # 更高概率的多余动作
    typing_wpm_range=(40, 60),          # 更慢的打字速度
    bezier_offset_range=(-120, 120),    # 更弯曲的鼠标轨迹
)
sim = HumanSimulator(config)
```

---

## 10. 故障排查

### 10.1 微信窗口未找到

```python
# 症状
RuntimeError: "微信窗口未找到，请确认微信已启动"

# 排查
1. 确认微信已启动且可见（非最小化）
2. 确认窗口类名正确: win32gui.FindWindow("WeChatMainWndForPC", None)
3. 如果微信 4.x 改变了类名，更新 config/settings.yaml 中的 window_class
```

### 10.2 C# UIA 服务不可用

```python
# 症状
logger.warning("WeChatUIA.exe 未找到")

# 排查
1. 确认已编译 C# 服务:
   cd src/cs_uia_service && dotnet publish -c Release -o publish

2. 确认 .NET 8.0 SDK 已安装:
   dotnet --version

3. 如果不想使用 C# 服务，系统自动回退到纯 Python OCR 模式（性能略降）
```

### 10.3 OCR 安装问题

```python
# PaddleOCR 安装
pip install paddlepaddle   # 先装 paddlepaddle
pip install paddleocr       # 再装 paddleocr

# 如果 PaddleOCR 安装失败
pip install easyocr         # 备选方案
# 然后修改 config/settings.yaml: ocr.engine = "easyocr"
```

### 10.4 OpenCV contrib 问题

```python
# 如果使用 SIFT（默认用 ORB 不需要）
# 问题: AttributeError: module 'cv2' has no attribute 'SIFT_create'
pip uninstall opencv-python
pip install opencv-contrib-python
```

### 10.5 日志分析

```python
# 启用 DEBUG 日志
# 修改 config/settings.yaml: logging.level = "DEBUG"

# 关键日志模式:
"OCR 扫描完成: N 个文本块"          — OCR 正常工作
"文本块数量异常少 (<5)"             — 窗口可能最小化
"等待 '[文字]' 超时"                — 页面未加载或不存在
"定位失败截图已保存"                — 查看 logs/failures/
```

### 10.6 调试截图

```
logs/failures/                     — 定位失败自动截图
debug_screenshots/                 — 手动调试截图

分析定位失败截图:
  1. 打开 PNG 查看当前屏幕状态
  2. 确认目标元素是否确实存在
  3. 如果存在但定位失败 → 调整 confidence 阈值
  4. 如果不存在 → 检查微信状态（掉线/弹窗/页面变化）
```

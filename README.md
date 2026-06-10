# 微信朋友圈自动化系统

版本无关的 PC 微信朋友圈自动化。事件驱动架构 + 语义级定位 + 三层集成。

[![Tests](https://img.shields.io/badge/tests-44%20passed-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.10+-blue)]()
[![.NET](https://img.shields.io/badge/.NET-10.0-purple)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()

---

## 快速开始

```bash
# 一键安装
setup.bat

# 系统自检
venv\Scripts\activate
python main.py --test

# 发朋友圈
python main.py --text "今天天气真好"

# 交互模式
python main.py --interactive

# 定时调度
python main.py --schedule

# 查看状态
python main.py --status
```

## 核心特性

| 特性 | 说明 |
|------|------|
| **版本无关** | OCR 文字语义 + SIFT/ORB 图形语义，不依赖微信控件树 |
| **事件驱动** | EventBus + Watcher 后台监测，等事件不等时间 |
| **类人行为** | Gamma 延迟 + 贝塞尔鼠标轨迹 + 键盘时序 + 随机多余动作 |
| **风控感知** | 自动检测风控信号 + 指数退避冷却 + 静默失效检测 |
| **多端控制** | CLI + REST API + WebSocket + Telegram/Discord (OpenClaw) + Electron GUI |
| **自动校准** | 启动时自动扫描界面建立锚点，窗口移动自动重新校准 |
| **版本追踪** | 检测微信版本变化，自动重建模板库 |
| **任务持久** | 断点续传 + 定时调度 (cron) + 发布历史 |
| **通知系统** | Telegram Bot + 邮件 + Windows 系统通知 |

## 架构

```
Telegram ←→ OpenClaw ←→ Python API Server ←→ Core Engine
Electron App ←→ Python API Server (HTTP + WebSocket)

Core Engine:
  EventBus → Watchers (OCR/UIA/Window/Timer)
           → Publisher (事件驱动状态机)
           → Locators (OCR → SIFT → Anchor → Template)
           → Monitors (Risk + Popup)
           → Recovery (多层级)
```

## 使用方式

```bash
# CLI
python main.py --text "文字内容" --images photo1.jpg
python main.py --batch posts.txt
python main.py --interactive
python main.py --schedule
python main.py --status
python main.py --calibrate
python main.py --extract-templates
python main.py --test

# API Server
python -m uvicorn src.api.server:app --port 18080

# API 端点
POST /api/publish        发布朋友圈
GET  /api/status         系统状态
POST /api/schedule       创建定时任务
GET  /api/history        发布历史
WS   /ws/events          实时事件流
```

## 三层集成

### OpenClaw — 消息平台远程控制

在 Telegram/WhatsApp/Discord 中：
```
/publish 今天天气真好 | photo.jpg
/status
/schedule add 0 9 * * * | 早安
/history
```

文件: `integrations/openclaw/wechat-moments-skill.ts`

### Electron 前端 — 基于 sparkle-ref

Dashboard + Composer + Schedule + History，集成指南:
`integrations/sparkle-frontend/INTEGRATION.md`

### API Server — FastAPI

10 个 REST 端点 + WebSocket 实时事件流，同时为 OpenClaw 和前端服务。
文件: `src/api/server.py`

## 环境要求

| 组件 | 要求 | 用途 |
|------|------|------|
| Python | 3.10+ | 核心引擎 |
| .NET SDK | 8.0+ (可选) | C# UIA 微服务 |
| 微信 | PC 版 3.9+ | 目标应用 |

## 安装

```bash
# 方式 1: 一键脚本
setup.bat

# 方式 2: 手动
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cd src/cs_uia_service && dotnet publish -c Release -o publish && cd ../..
python main.py --test
```

## 配置

编辑 `config/settings.yaml`：

- OCR 引擎选择 (paddleocr / wechat_native)
- 类人行为参数 (延迟、鼠标、打字速度)
- 风控安全 (每日上限、冷却基数)
- 通知渠道 (Telegram Bot、邮件)

## 测试

```bash
python -m pytest tests/ -v
# 44 passed, 0 failed
```

## 文件结构

```
src/core/        事件驱动核心 (EventBus + Watchers + Publisher)
src/api/         FastAPI Server
src/locator/     版本无关定位 (7 模块)
src/executor/    执行层 (6 模块)
src/monitor/     监控层 (3 模块)
src/recovery/    恢复层
src/cs_uia_service/  C# UIA 微服务
integrations/    外部集成 (OpenClaw + Electron)
tests/           测试 (44 tests)
```

## 免责声明

本项目仅供技术研究和学习使用。使用自动化工具操作微信可能违反《腾讯微信软件许可及服务协议》，使用者需自行承担风险。

## License

MIT

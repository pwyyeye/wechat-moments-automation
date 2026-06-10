# 集成架构设计 — OpenClaw + Python API + Electron 前端

## 整体架构

```
用户交互层:
  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐
  │ Telegram │  │ WhatsApp │  │ Discord  │  │ Electron Desktop │
  │ "发朋友圈"│  │ "发朋友圈"│  │ "发朋友圈"│  │ (sparkle-ref)    │
  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬─────────┘
       │              │             │                  │ HTTP REST
       └──────────────┼─────────────┘                  │
                      │                                │
              ┌───────▼────────┐              ┌────────▼────────┐
              │   OpenClaw     │              │  React Frontend │
              │   Gateway      │   HTTP API   │  (新增页面)     │
              │                │◄────────────►│                 │
              │ WeChat Moments │              │ · Dashboard    │
              │ Skill          │              │ · Composer     │
              └───────┬────────┘              │ · Schedule     │
                      │                       │ · Templates    │
                      │ HTTP REST             │ · History      │
                      │                       │ · Settings     │
              ┌───────▼────────────────────────▼────────┐
              │        Python API Server (FastAPI)      │
              │                                        │
              │  POST /api/publish      发布朋友圈      │
              │  GET  /api/status       系统状态        │
              │  POST /api/schedule     创建定时任务    │
              │  GET  /api/history      发布历史        │
              │  POST /api/templates    管理模板        │
              │  GET  /api/logs         运行日志        │
              │  WS   /ws/events        实时事件流      │
              └────────────────┬───────────────────────┘
                               │ Python import
              ┌────────────────▼───────────────────────┐
              │     Core Engine (已有)                  │
              │  src/core/    — EventBus + Publisher    │
              │  src/locator/ — OCR + SIFT + Anchor     │
              │  src/executor/— HumanSim + Operator     │
              │  src/monitor/ — Risk + Popup            │
              └────────────────────────────────────────┘
```

## 各层职责

### OpenClaw Skill

- **位置**: `integrations/openclaw/wechat-moments-skill.ts`
- **职责**: 接收来自 Telegram/WhatsApp/Discord 的自然语言指令，转换为 API 调用
- **协议**: 调用 Python API Server 的 HTTP 端点

### Python API Server

- **位置**: `src/api/server.py`
- **框架**: FastAPI + uvicorn
- **职责**: 将核心引擎封装为 REST API，同时为 OpenClaw 和 Electron 前端服务
- **端口**: 默认 18080

### Electron 前端页面

- **位置**: `integrations/sparkle-frontend/`
- **框架**: React 19 + TypeScript + HeroUI + Tailwind CSS（与 sparkle-ref 一致）
- **集成方式**: 作为 sparkle-ref 项目的独立路由模块
- **需要新增的页面**: Dashboard, Composer, Schedule, Templates, History, Settings

## API 设计

### POST /api/publish

```json
// Request
{
  "text": "今天天气真好",
  "images": ["C:\\photos\\sunset.jpg"],
  "schedule_at": null
}

// Response 200
{
  "success": true,
  "task_id": "task_20260611_001",
  "elapsed_seconds": 12.5,
  "step_times": {
    "enter_moments": 2.1,
    "type_text": 1.2,
    "publish": 3.1
  }
}
```

### GET /api/status

```json
// Response 200
{
  "status": "running",
  "wechat": {
    "logged_in": true,
    "window_visible": true
  },
  "risk": {
    "level": "SAFE",
    "consecutive_events": 0
  },
  "daily": {
    "posts_used": 3,
    "posts_limit": 10
  },
  "templates_count": 12,
  "uptime_seconds": 3600
}
```

### WebSocket /ws/events

```
→ 实时推送系统事件流
  {"type": "step.started", "step": "publish", "timestamp": ...}
  {"type": "text.appeared", "text": "已发送", "timestamp": ...}
  {"type": "risk.warning", "level": "WARNING", "timestamp": ...}
  {"type": "step.completed", "elapsed": 12.5, "timestamp": ...}
```

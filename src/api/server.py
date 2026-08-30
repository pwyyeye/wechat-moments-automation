"""
Python API Server — FastAPI REST + WebSocket，连接 OpenClaw 和 Electron 前端。

端点：
  POST /api/publish        — 发布朋友圈
  GET  /api/status         — 系统状态
  POST /api/schedule       — 创建定时任务
  GET  /api/schedule       — 查看定时任务
  DELETE /api/schedule/{id}— 取消定时任务
  GET  /api/history        — 发布历史
  POST /api/templates/scan — 扫描并更新模板
  GET  /api/logs           — 运行日志
  WS   /ws/events          — 实时事件流

启动：
  uvicorn src.api.server:app --host 127.0.0.1 --port 18080

Author: 版本无关微信自动化系统
"""

import json
import logging
import sys
import time
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

class PublishRequest(BaseModel):
    text: str = Field(..., description="朋友圈文字内容", max_length=2000)
    images: List[str] = Field(default_factory=list, description="图片路径列表")
    schedule_at: Optional[str] = Field(None, description="定时发布时间 ISO 8601")
    confirm_publish: bool = Field(
        False,
        description="必须显式设为 true 才允许点击发表；默认停在编辑页",
    )

class PublishResponse(BaseModel):
    success: bool
    task_id: Optional[str] = None
    elapsed_seconds: float = 0
    step_times: Dict[str, float] = Field(default_factory=dict)
    error: str = ""
    published: bool = False
    stopped_before_publish: bool = False

class StatusResponse(BaseModel):
    status: str  # "running" | "idle" | "error"
    version: str = "0.1.0"
    wechat: Dict[str, Any] = Field(default_factory=dict)
    risk: Dict[str, Any] = Field(default_factory=dict)
    daily: Dict[str, Any] = Field(default_factory=dict)
    templates_count: int = 0
    uptime_seconds: float = 0

class ScheduleRequest(BaseModel):
    text: str = Field(..., max_length=2000)
    images: List[str] = Field(default_factory=list)
    cron: str = Field(..., description="cron 表达式，如 '0 9 * * *'")
    enabled: bool = True
    confirm_publish: bool = False

class ScheduleItem(BaseModel):
    id: str
    text: str
    images: List[str]
    cron: str
    enabled: bool
    created_at: str
    next_run: Optional[str] = None
    confirm_publish: bool = False

class HistoryItem(BaseModel):
    task_id: str
    text: str
    success: bool
    elapsed_seconds: float
    timestamp: str
    error: str = ""
    published: bool = False

# ═══════════════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════════════

class AppState:
    def __init__(self):
        self.publisher = None  # EventDrivenPublisher 实例
        self.start_time = time.time()
        self.event_clients: List[WebSocket] = []
        self.history: List[HistoryItem] = []
        self.schedules: Dict[str, ScheduleItem] = {}
        self._task_counter = 0

    def next_task_id(self) -> str:
        self._task_counter += 1
        return f"task_{datetime.now().strftime('%Y%m%d')}_{self._task_counter:03d}"

state = AppState()


# ═══════════════════════════════════════════════════════════════
# 应用生命周期
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭生命周期"""
    # 启动时：初始化 Publisher
    try:
        from src.core.publisher import EventDrivenPublisher, PublishTask
        from src.core.events import global_event_bus, EventType

        state.publisher = EventDrivenPublisher()

        if state.publisher.initialize():
            logger.info("Publisher 初始化完成")
        else:
            logger.warning("Publisher 初始化部分失败，部分功能可能不可用")

        # 订阅核心事件 → 转发到 WebSocket
        global_event_bus.on(EventType.STEP_STARTED,
                            lambda e: asyncio_run(_broadcast_event(e)))
        global_event_bus.on(EventType.STEP_COMPLETED,
                            lambda e: asyncio_run(_broadcast_event(e)))
        global_event_bus.on(EventType.STEP_FAILED,
                            lambda e: asyncio_run(_broadcast_event(e)))
        global_event_bus.on(EventType.RISK_WARNING,
                            lambda e: asyncio_run(_broadcast_event(e)))

        logger.info("事件转发已注册")
    except Exception as e:
        logger.error(f"初始化失败: {e}")

    yield

    # 关闭时
    if state.publisher:
        state.publisher.shutdown()
    logger.info("API Server 已关闭")


def asyncio_run(coro):
    """在同步上下文中安全调度 asyncio 协程"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(coro)
        else:
            loop.run_until_complete(coro)
    except RuntimeError:
        pass


async def _broadcast_event(event):
    """将事件广播到所有 WebSocket 客户端"""
    dead = []
    payload = {
        'type': event.type.value,
        'source': event.source,
        'payload': event.payload,
        'timestamp': event.timestamp,
    }
    for ws in state.event_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.event_clients.remove(ws)


# ═══════════════════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="WeChat Moments Automation API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════

@app.post("/api/publish", response_model=PublishResponse)
async def api_publish(req: PublishRequest):
    """发布朋友圈"""
    if state.publisher is None:
        raise HTTPException(503, "Publisher 未初始化")

    from src.core.publisher import PublishTask

    task = PublishTask(
        text=req.text,
        images=req.images,
        confirm_publish=req.confirm_publish,
    )
    result = state.publisher.publish(task)

    # 记录历史
    history_item = HistoryItem(
        task_id=state.next_task_id(),
        text=req.text[:50],
        success=result.success,
        elapsed_seconds=result.elapsed_seconds,
        timestamp=datetime.now().isoformat(),
        error=result.error_message,
        published=result.published,
    )
    state.history.insert(0, history_item)
    if len(state.history) > 500:
        state.history = state.history[:500]

    return PublishResponse(
        success=result.success,
        task_id=history_item.task_id,
        elapsed_seconds=result.elapsed_seconds,
        step_times=result.step_times,
        error=result.error_message,
        published=result.published,
        stopped_before_publish=result.stopped_before_publish,
    )


@app.get("/api/status", response_model=StatusResponse)
async def api_status():
    """系统状态"""
    wechat_info = {"logged_in": False, "window_visible": False}
    risk_info = {"level": "UNKNOWN", "consecutive_events": 0}
    daily_info = {"posts_used": 0, "posts_limit": 10}

    if state.publisher:
        try:
            login = state.publisher.operator.check_login_state()
            wechat_info = {
                "logged_in": login.get('logged_in', False),
                "page": login.get('page', 'unknown'),
                "window_visible": True,
            }
        except Exception:
            pass

        try:
            risk = state.publisher.risk_detector.state
            risk_info = {
                "level": risk.level.name,
                "consecutive_events": risk.consecutive_events,
                "cooldown_remaining": max(0, risk.cooldown_until - time.time()),
            }
        except Exception:
            pass

    return StatusResponse(
        status="running" if state.publisher else "idle",
        wechat=wechat_info,
        risk=risk_info,
        daily=daily_info,
        templates_count=0,
        uptime_seconds=time.time() - state.start_time,
    )


@app.post("/api/schedule", response_model=ScheduleItem)
async def api_create_schedule(req: ScheduleRequest):
    """创建定时发布任务"""
    schedule_id = state.next_task_id()

    # 解析 cron 表达式，计算下次运行时间
    try:
        from croniter import croniter
        base = datetime.now()
        cron = croniter(req.cron, base)
        next_run = cron.get_next(datetime).isoformat()
    except ImportError:
        next_run = None  # croniter 不可用时跳过

    item = ScheduleItem(
        id=schedule_id,
        text=req.text,
        images=req.images,
        cron=req.cron,
        enabled=req.enabled,
        created_at=datetime.now().isoformat(),
        next_run=next_run,
        confirm_publish=req.confirm_publish,
    )
    state.schedules[schedule_id] = item

    return item


@app.get("/api/schedule", response_model=List[ScheduleItem])
async def api_list_schedules():
    """查看所有定时任务"""
    return list(state.schedules.values())


@app.delete("/api/schedule/{schedule_id}")
async def api_delete_schedule(schedule_id: str):
    """取消定时任务"""
    if schedule_id in state.schedules:
        del state.schedules[schedule_id]
        return {"success": True}
    raise HTTPException(404, "定时任务不存在")


@app.get("/api/history", response_model=List[HistoryItem])
async def api_history(limit: int = Query(50, le=500)):
    """发布历史"""
    return state.history[:limit]


@app.post("/api/templates/scan")
async def api_scan_templates():
    """扫描并更新图标模板"""
    try:
        from src.locator.template_extractor import update_all_templates
        count = update_all_templates(
            state.publisher.ocr if state.publisher else None
        )
        return {"success": True, "count": count}
    except Exception as e:
        raise HTTPException(500, f"模板扫描失败: {e}")


@app.get("/api/logs")
async def api_logs(lines: int = Query(100, le=1000)):
    """获取最近日志"""
    from pathlib import Path
    log_dir = Path("logs")
    if not log_dir.exists():
        return {"lines": []}

    log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        return {"lines": []}

    with open(log_files[0], 'r', encoding='utf-8', errors='replace') as f:
        all_lines = f.readlines()
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines

    return {"file": str(log_files[0]), "lines": [l.rstrip() for l in recent]}


# ═══════════════════════════════════════════════════════════════
# WebSocket — 实时事件流
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """实时事件流 WebSocket"""
    await ws.accept()
    state.event_clients.append(ws)

    # 发送初始状态
    await ws.send_json({
        'type': 'connected',
        'message': '已连接到 WeChat Moments API',
        'timestamp': time.time(),
    })

    try:
        while True:
            # 保持连接，等待客户端消息（心跳）
            data = await ws.receive_text()
            if data == 'ping':
                await ws.send_json({'type': 'pong', 'timestamp': time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in state.event_clients:
            state.event_clients.remove(ws)


# ═══════════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 多账号管理
# ═══════════════════════════════════════════════════════════════

@app.get("/api/accounts")
async def api_accounts():
    """列出所有可用的微信账号"""
    try:
        from src.core.account_manager import WeChatWindowFinder
        windows = WeChatWindowFinder.enum_all()
        accounts = []
        for hwnd, title in windows:
            info = WeChatWindowFinder.get_window_info(hwnd)
            if info:
                accounts.append({
                    'name': info.name,
                    'process_id': info.process_id,
                    'is_minimized': info.is_minimized,
                    'is_visible': info.is_visible,
                })
        return {"accounts": accounts, "count": len(accounts)}
    except Exception as e:
        return {"accounts": [], "count": 0, "error": str(e)}


@app.post("/api/accounts/{name}/publish")
async def api_account_publish(name: str, req: PublishRequest):
    """在指定账号上发布朋友圈"""
    if state.publisher is None:
        raise HTTPException(503, "Publisher 未初始化")
    # TODO: 集成 AccountManager 的多账号发布
    # 当前回退到默认 publisher
    from src.core.publisher import PublishTask
    task = PublishTask(
        text=req.text,
        images=req.images,
        confirm_publish=req.confirm_publish,
    )
    result = state.publisher.publish(task)
    return PublishResponse(
        success=result.success,
        elapsed_seconds=getattr(result, 'elapsed_seconds', 0),
        error=getattr(result, 'error_message', ''),
        published=getattr(result, 'published', False),
        stopped_before_publish=getattr(result, 'stopped_before_publish', False),
    )


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": time.time()}


# ═══════════════════════════════════════════════════════════════
# 直接运行
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="info")

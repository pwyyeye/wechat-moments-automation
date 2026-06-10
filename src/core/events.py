"""
事件系统 —— 发布/订阅事件总线，事件驱动的核心。

设计原则：
  1. 一切 UI 变化都是事件（按钮出现、文字出现、窗口移动、弹窗弹出）
  2. 消费者订阅事件，生产者发布事件
  3. 不轮询、不等固定延迟 —— 事件到达时立即响应
  4. 事件带时间戳，支持超时检测

事件分类：
  ┌─────────────────┬──────────────────────────────────────┐
  │ 类别            │ 事件示例                             │
  ├─────────────────┼──────────────────────────────────────┤
  │ UI 元素事件     │ element.appeared, element.vanished   │
  │ 文字事件        │ text.appeared, text.changed          │
  │ 窗口事件        │ window.moved, window.resized         │
  │ 状态事件        │ login.lost, login.restored           │
  │ 风控事件        │ risk.warning, risk.critical          │
  │ 弹窗事件        │ popup.detected, popup.dismissed      │
  │ 流程事件        │ step.completed, step.failed          │
  │ 定时器事件      │ timer.timeout                        │
  └─────────────────┴──────────────────────────────────────┘

Author: 版本无关微信自动化系统
"""

import time
import logging
import threading
from enum import Enum
from typing import Optional, Callable, Dict, List, Any, Set
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 事件定义
# ═══════════════════════════════════════════════════════════════

class EventType(Enum):
    """系统事件类型枚举"""

    # ── UI 元素事件 ──
    ELEMENT_APPEARED = "element.appeared"       # 元素出现在界面上
    ELEMENT_VANISHED = "element.vanished"        # 元素从界面消失
    ELEMENT_CHANGED = "element.changed"          # 元素属性变化

    # ── 文字事件（OCR） ──
    TEXT_APPEARED = "text.appeared"              # 指定文字出现在屏幕上
    TEXT_VANISHED = "text.vanished"              # 指定文字从屏幕消失

    # ── 窗口事件 ──
    WINDOW_MOVED = "window.moved"                # 窗口位置变化
    WINDOW_RESIZED = "window.resized"            # 窗口大小变化
    WINDOW_MINIMIZED = "window.minimized"        # 窗口最小化
    WINDOW_RESTORED = "window.restored"          # 窗口恢复

    # ── 登录状态 ──
    LOGIN_LOST = "login.lost"                    # 掉线/强制登出
    LOGIN_RESTORED = "login.restored"            # 重新登录成功

    # ── 风控 ──
    RISK_WARNING = "risk.warning"                # 风控警告（操作频繁）
    RISK_CRITICAL = "risk.critical"              # 风控严重（被限制）
    RISK_COOLDOWN_START = "risk.cooldown.start"  # 冷却开始
    RISK_COOLDOWN_END = "risk.cooldown.end"      # 冷却结束

    # ── 弹窗 ──
    POPUP_DETECTED = "popup.detected"            # 检测到弹窗
    POPUP_DISMISSED = "popup.dismissed"          # 弹窗已关闭

    # ── 流程事件 ──
    STEP_STARTED = "step.started"                # 步骤开始
    STEP_COMPLETED = "step.completed"            # 步骤完成
    STEP_FAILED = "step.failed"                  # 步骤失败
    STEP_RETRY = "step.retry"                    # 步骤重试

    # ── 发布流程 ──
    PUBLISH_CONFIRMED = "publish.confirmed"      # 发布确认
    UPLOAD_COMPLETE = "upload.complete"          # 图片上传完成
    UPLOAD_FAILED = "upload.failed"              # 图片上传失败

    # ── 定时器 ──
    TIMER_EXPIRED = "timer.expired"              # 定时器到期

    # ── 系统 ──
    SYSTEM_ERROR = "system.error"                # 系统错误
    SYSTEM_SHUTDOWN = "system.shutdown"          # 系统关闭


@dataclass
class Event:
    """
    事件对象。

    每个事件携带：
      - type: 事件类型
      - source: 事件来源组件名
      - payload: 事件携带的数据
      - timestamp: 事件发生的时间戳
    """
    type: EventType
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __repr__(self):
        payload_str = ', '.join(f'{k}={v}' for k, v in self.payload.items())
        return f"<Event {self.type.value} from={self.source} [{payload_str}]>"


# ═══════════════════════════════════════════════════════════════
# 事件总线
# ═══════════════════════════════════════════════════════════════

class EventBus:
    """
    事件总线 —— 发布/订阅核心。

    特性：
      - 多对多：任意数量的发布者和订阅者
      - 通配符订阅：订阅 "step.*" 接收所有步骤事件
      - 一次性订阅：once() 方法，收到一次后自动取消
      - 等待事件：wait_for() 阻塞直到指定事件到达（带超时）
      - 历史记录：保留最近 N 个事件用于调试

    使用方式：
        bus = EventBus()

        # 订阅
        bus.on(EventType.ELEMENT_APPEARED, lambda e: print(f"出现: {e}"))

        # 等待（事件驱动替代 time.sleep）
        event = bus.wait_for(EventType.TEXT_APPEARED, timeout=10.0)

        # 发布
        bus.emit(Event(EventType.STEP_COMPLETED, "publisher", {"step": "typing"}))
    """

    def __init__(self, history_size: int = 200):
        self._subscribers: Dict[EventType, List[Callable]] = defaultdict(list)
        self._wildcard_subscribers: List[Callable] = []
        self._waiters: Dict[str, List[tuple]] = defaultdict(list)
        self._history: List[Event] = []
        self._history_max = history_size
        self._lock = threading.Lock()
        self._waiter_lock = threading.Lock()

    # ── 发布 ──

    def emit(self, event: Event):
        """发布事件到所有订阅者"""
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_max:
                self._history = self._history[-self._history_max:]

        logger.debug(str(event))

        # 通知具体订阅者
        for handler in self._subscribers[event.type]:
            self._safe_call(handler, event)

        # 通知通配符订阅者
        for handler in self._wildcard_subscribers:
            self._safe_call(handler, event)

        # 通知 wait_for 等待者
        self._notify_waiters(event)

    def _safe_call(self, handler: Callable, event: Event):
        """安全调用处理器（异常不中断其他处理器）"""
        try:
            handler(event)
        except Exception as e:
            logger.error(f"事件处理器异常 [{event.type.value}]: {e}")

    # ── 订阅 ──

    def on(self, event_type: EventType, handler: Callable[[Event], None]):
        """订阅特定事件类型"""
        self._subscribers[event_type].append(handler)

    def on_any(self, handler: Callable[[Event], None]):
        """订阅所有事件（通配符）"""
        self._wildcard_subscribers.append(handler)

    def once(self, event_type: EventType, handler: Callable[[Event], None]):
        """一次性订阅：收到后自动取消"""
        wrapper = [None]  # 用列表包装避免闭包问题

        def _wrapper(event):
            self.off(event_type, _wrapper)
            handler(event)

        wrapper[0] = _wrapper
        self.on(event_type, _wrapper)

    def off(self, event_type: EventType, handler: Callable):
        """取消订阅"""
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    # ── 等待（事件驱动的时间控制） ──

    def wait_for(self, event_type: EventType,
                 payload_match: Dict[str, Any] = None,
                 timeout: float = 30.0) -> Optional[Event]:
        """
        阻塞等待指定事件到达。

        这是事件驱动架构中替代 time.sleep() 的核心方法。
        不会轮询，由 event emit 时主动唤醒。

        Args:
            event_type: 等待的事件类型
            payload_match: 可选的载荷匹配条件，如 {'text': '已发送'}
            timeout: 超时时间（秒）

        Returns:
            接收到的事件，超时返回 None
        """
        event_holder = threading.Event()
        result_holder: List[Optional[Event]] = [None]

        def _waiter(event: Event):
            if payload_match:
                # 检查载荷是否匹配
                for key, val in payload_match.items():
                    if event.payload.get(key) != val:
                        return
            result_holder[0] = event
            event_holder.set()

        with self._waiter_lock:
            waiter_id = f"{event_type.value}_{id(_waiter)}"
            self._waiters[event_type.value].append((waiter_id, _waiter))

        # 阻塞等待
        received = event_holder.wait(timeout=timeout)

        # 清理
        with self._waiter_lock:
            self._waiters[event_type.value] = [
                (wid, w) for wid, w in self._waiters[event_type.value]
                if wid != waiter_id
            ]

        if not received:
            logger.warning(f"等待事件 {event_type.value} 超时 ({timeout}s)")

        return result_holder[0]

    def wait_any(self, event_types: List[EventType],
                 timeout: float = 30.0) -> Optional[Event]:
        """等待任意一个事件到达"""
        event_holder = threading.Event()
        result_holder: List[Optional[Event]] = [None]

        def _waiter(event: Event):
            result_holder[0] = event
            event_holder.set()

        for et in event_types:
            self.once(et, _waiter)

        received = event_holder.wait(timeout=timeout)
        return result_holder[0] if received else None

    # ── 查询 ──

    def history(self, event_type: EventType = None,
                limit: int = 50) -> List[Event]:
        """获取事件历史（调试用）"""
        with self._lock:
            events = list(self._history)
        if event_type:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]

    def last(self, event_type: EventType) -> Optional[Event]:
        """获取指定类型的最近一个事件"""
        with self._lock:
            for event in reversed(self._history):
                if event.type == event_type:
                    return event
        return None

    # ── 内部 ──

    def _notify_waiters(self, event: Event):
        """通知所有等待该事件类型的 waiter"""
        with self._waiter_lock:
            waiters = self._waiters.get(event.type.value, [])

        for _, waiter in waiters:
            try:
                waiter(event)
            except Exception as e:
                logger.debug(f"Waiter 异常: {e}")

    def shutdown(self):
        """关闭事件总线，释放所有等待者"""
        with self._lock:
            self._subscribers.clear()
            self._wildcard_subscribers.clear()

        with self._waiter_lock:
            for waiters in self._waiters.values():
                for _, _ in waiters:
                    pass
            self._waiters.clear()

        logger.info("事件总线已关闭")


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

# 全局事件总线实例（整个应用共享一个 bus）
global_event_bus = EventBus()

"""
事件源（Watcher）—— 在后台持续监测，发现变化立即发布事件。

每个 Watcher 是一个独立线程，监测一个事件维度：
  UIATreeWatcher  —— 监测 UIA 控件树变化，发布 element.* 事件
  OCRTextWatcher  —— 监测屏幕上的文字变化，发布 text.* 事件
  WindowWatcher  —— 监测窗口位置/状态变化，发布 window.* 事件
  LoginWatcher   —— 监测登录状态，发布 login.* 事件
  TimerWatcher   —— 发布 timer.expired 事件（事件驱动的 timeout）

Watcher 不轮询结果，而是观察变化：
  - UIA: 对比前后两次控件树快照的 diff
  - OCR: 对比前后两次文字扫描结果的 diff
  - Window: 通过 SetWinEventHook 原生回调（零轮询）
  - Timer: 单独的定时线程

Author: 版本无关微信自动化系统
"""

import time
import logging
import threading
from typing import Optional, Set, List, Dict, Any, Callable
from dataclasses import dataclass

from .events import EventBus, Event, EventType

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════

class BaseWatcher:
    """Watcher 基类"""

    def __init__(self, bus: EventBus, name: str, interval: float = 0.5):
        self.bus = bus
        self.name = name
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """启动后台监测"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.debug(f"Watcher 启动: {self.name}")

    def stop(self):
        """停止监测"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.debug(f"Watcher 停止: {self.name}")

    def _run_loop(self):
        """子类重写此方法实现监测逻辑"""
        pass


# ═══════════════════════════════════════════════════════════════
# OCR 文字 Watcher —— 监测屏幕上的文字出现/消失
# ═══════════════════════════════════════════════════════════════

class OCRTextWatcher(BaseWatcher):
    """
    监测屏幕文字变化 —— 发现目标文字出现/消失时发布事件。

    原理：
      - 定期 OCR 扫描屏幕（可配置间隔）
      - 对比当前和上一帧的文字集合
      - 新出现的文字 → emit TEXT_APPEARED
      - 消失的文字 → emit TEXT_VANISHED

    使用方式：
        watcher = OCRTextWatcher(bus, ocr_locator, watch_texts=["已发送", "操作太频繁"])
        watcher.start()

        # 在别处等待事件
        event = bus.wait_for(EventType.TEXT_APPEARED,
                             payload_match={'text': '已发送'})
    """

    def __init__(self, bus: EventBus, ocr_locator,
                 watch_texts: List[str] = None,
                 interval: float = 1.0):
        super().__init__(bus, "OCRTextWatcher", interval)
        self._ocr = ocr_locator
        self._watch_texts = set(watch_texts or [])
        self._last_texts: Set[str] = set()

    def watch_for(self, texts: List[str]):
        """添加要监测的文字"""
        self._watch_texts.update(texts)

    def unwatch(self, texts: List[str]):
        """移除监测的文字"""
        self._watch_texts.difference_update(texts)

    def _run_loop(self):
        while self._running:
            try:
                # OCR 扫描
                blocks = self._ocr.scan_screen()
                current_texts = {b.text.strip() for b in blocks}

                # Diff: 新出现的
                appeared = current_texts - self._last_texts
                for text in appeared:
                    # 只报告我们关心的文字
                    for watch_text in self._watch_texts:
                        if watch_text in text:
                            # 找到精确的坐标
                            for block in blocks:
                                if watch_text in block.text:
                                    self.bus.emit(Event(
                                        EventType.TEXT_APPEARED,
                                        self.name,
                                        {
                                            'text': text,
                                            'matched': watch_text,
                                            'x': block.x, 'y': block.y,
                                            'confidence': block.confidence,
                                        }
                                    ))
                                    break

                # Diff: 消失的
                vanished = self._last_texts - current_texts
                for text in vanished:
                    for watch_text in self._watch_texts:
                        if watch_text in text:
                            self.bus.emit(Event(
                                EventType.TEXT_VANISHED,
                                self.name,
                                {'text': text, 'matched': watch_text}
                            ))

                self._last_texts = current_texts

            except Exception as e:
                logger.debug(f"OCR Watcher 异常: {e}")

            time.sleep(self.interval)


# ═══════════════════════════════════════════════════════════════
# UIA 控件树 Watcher —— 监测控件变化
# ═══════════════════════════════════════════════════════════════

class UIATreeWatcher(BaseWatcher):
    """
    监测 UIA 控件树变化 —— 发现目标控件出现/消失时发布事件。

    这比 OCR Watcher 更快更准（如果 UIA 可用的话）。

    使用方式：
        watcher = UIATreeWatcher(bus, uia_bridge,
                                 watch_elements=["朋友圈", "发表"])
        watcher.start()
    """

    def __init__(self, bus: EventBus, uia_bridge,
                 watch_names: List[str] = None,
                 interval: float = 0.5):
        super().__init__(bus, "UIATreeWatcher", interval)
        self._uia = uia_bridge
        self._watch_names = set(watch_names or [])
        self._last_elements: Set[str] = set()

    def watch_for(self, names: List[str]):
        self._watch_names.update(names)

    def _run_loop(self):
        while self._running:
            if not self._uia or not self._uia.available:
                time.sleep(2.0)
                continue

            try:
                # 获取当前界面上所有可交互元素的名称
                elements = self._uia.get_all_interactable() if hasattr(
                    self._uia, 'get_all_interactable'
                ) else []

                current_names = {
                    e.get('name', '') for e in elements
                    if e.get('name')
                }

                # 新出现的
                appeared = current_names - self._last_elements
                for name in appeared:
                    for watch_name in self._watch_names:
                        if watch_name in name:
                            for elem in elements:
                                if elem.get('name') == name:
                                    self.bus.emit(Event(
                                        EventType.ELEMENT_APPEARED,
                                        self.name,
                                        {
                                            'name': name,
                                            'matched': watch_name,
                                            'x': elem.get('x', 0) + elem.get('width', 0) // 2,
                                            'y': elem.get('y', 0) + elem.get('height', 0) // 2,
                                            'controlType': elem.get('controlType', ''),
                                        }
                                    ))
                                    break

                self._last_elements = current_names

            except Exception as e:
                logger.debug(f"UIA Watcher 异常: {e}")

            time.sleep(self.interval)


# ═══════════════════════════════════════════════════════════════
# 窗口 Watcher —— 通过 C# 微服务的原生 WinEventHook 回调
# ═══════════════════════════════════════════════════════════════

class WindowWatcher:
    """
    窗口状态监测 —— 零轮询，通过 C# WinEventHook 原生回调。

    使用 SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE) 在操作系统层面
    注册窗口变化回调。操作系统通知时立即发布事件，无轮询开销。

    使用方式：
        watcher = WindowWatcher(bus, uia_bridge)
        watcher.start()
    """

    def __init__(self, bus: EventBus, uia_bridge):
        self.bus = bus
        self._uia = uia_bridge
        self._last_rect = None

    def start(self):
        """启动窗口监控"""
        if not self._uia or not self._uia.available:
            logger.warning("UIA 不可用，窗口监控未启动")
            return

        self._last_rect = self._uia.get_window_rect()

        def _on_change(window_info: dict):
            new_rect = (
                window_info.get('left', 0),
                window_info.get('top', 0),
                window_info.get('right', 0) - window_info.get('left', 0),
                window_info.get('bottom', 0) - window_info.get('top', 0),
            )

            old_rect = self._last_rect
            if old_rect:
                dx = new_rect[0] - old_rect[0]
                dy = new_rect[1] - old_rect[1]
                dw = new_rect[2] - old_rect[2]
                dh = new_rect[3] - old_rect[3]

                if abs(dx) > 3 or abs(dy) > 3:
                    self.bus.emit(Event(
                        EventType.WINDOW_MOVED, "WindowWatcher",
                        {'left': new_rect[0], 'top': new_rect[1], 'dx': dx, 'dy': dy}
                    ))

                if abs(dw) > 5 or abs(dh) > 5:
                    self.bus.emit(Event(
                        EventType.WINDOW_RESIZED, "WindowWatcher",
                        {'width': new_rect[2], 'height': new_rect[3], 'dw': dw, 'dh': dh}
                    ))

                if window_info.get('isMinimized'):
                    self.bus.emit(Event(
                        EventType.WINDOW_MINIMIZED, "WindowWatcher", {}
                    ))

            self._last_rect = new_rect

        self._uia.start_window_monitor(on_change=_on_change, run_in_background=True)
        logger.info("窗口监控已启动（WinEventHook 零轮询）")

    def stop(self):
        if self._uia:
            self._uia.stop_window_monitor()


# ═══════════════════════════════════════════════════════════════
# 定时器 —— 事件驱动的时间控制
# ═══════════════════════════════════════════════════════════════

class TimerWatcher:
    """
    事件驱动的定时器。

    替代 time.sleep() 和固定延迟。发布 timer.expired 事件，
    其他组件监听该事件做后续处理。

    使用方式：
        timer = TimerWatcher(bus)

        # 3 秒后发布事件
        timer.after(3.0, {'reason': 'wait_for_animation'})

        # 在别处等待
        event = bus.wait_for(EventType.TIMER_EXPIRED, timeout=5.0)
    """

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._timers: List[threading.Timer] = []

    def after(self, seconds: float, payload: Dict[str, Any] = None):
        """
        在指定秒数后发布 timer.expired 事件。

        Args:
            seconds: 延迟秒数
            payload: 附加到事件的载荷
        """
        timer = threading.Timer(seconds, self._on_timer, args=[payload or {}])
        timer.daemon = True
        timer.start()
        self._timers.append(timer)

    def _on_timer(self, payload: Dict[str, Any]):
        self.bus.emit(Event(
            EventType.TIMER_EXPIRED, "TimerWatcher", payload
        ))

    def cancel_all(self):
        """取消所有待处理的定时器"""
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()


# ═══════════════════════════════════════════════════════════════
# 合并 Watcher —— 统一管理所有事件源
# ═══════════════════════════════════════════════════════════════

class WatchManager:
    """
    统一管理所有 Watcher 的生命周期。

    使用方式：
        manager = WatchManager(bus, ocr_locator, uia_bridge)
        manager.start_all()
        # ... 系统运行 ...
        manager.stop_all()
    """

    def __init__(self, bus: EventBus, ocr_locator=None, uia_bridge=None):
        self.bus = bus
        self.ocr_locator = ocr_locator
        self.uia_bridge = uia_bridge

        # Watcher 实例
        self.ocr_watcher: Optional[OCRTextWatcher] = None
        self.uia_watcher: Optional[UIATreeWatcher] = None
        self.window_watcher: Optional[WindowWatcher] = None
        self.timer: TimerWatcher = TimerWatcher(bus)

    def start_all(self,
                  watch_ocr_texts: List[str] = None,
                  watch_uia_names: List[str] = None):
        """启动所有 Watcher"""

        # OCR Watcher
        if self.ocr_locator:
            self.ocr_watcher = OCRTextWatcher(
                self.bus, self.ocr_locator,
                watch_texts=watch_ocr_texts or [],
                interval=1.0,
            )
            self.ocr_watcher.start()

        # UIA Watcher
        if self.uia_bridge and self.uia_bridge.available:
            self.uia_watcher = UIATreeWatcher(
                self.bus, self.uia_bridge,
                watch_names=watch_uia_names or [],
                interval=0.5,
            )
            self.uia_watcher.start()

        # 窗口 Watcher
        if self.uia_bridge and self.uia_bridge.available:
            self.window_watcher = WindowWatcher(self.bus, self.uia_bridge)
            self.window_watcher.start()

        logger.info(
            f"WatchManager 已启动 "
            f"(OCR={self.ocr_watcher is not None}, "
            f"UIA={self.uia_watcher is not None}, "
            f"Window={self.window_watcher is not None})"
        )

    def stop_all(self):
        """停止所有 Watcher"""
        for watcher in [self.ocr_watcher, self.uia_watcher, self.window_watcher]:
            if watcher:
                watcher.stop()
        self.timer.cancel_all()
        logger.info("WatchManager 已停止")

    def watch_text(self, texts: List[str]):
        """动态添加 OCR 监测目标"""
        if self.ocr_watcher:
            self.ocr_watcher.watch_for(texts)

    def watch_element(self, names: List[str]):
        """动态添加 UIA 监测目标"""
        if self.uia_watcher:
            self.uia_watcher.watch_for(names)

    def after(self, seconds: float, payload: Dict[str, Any] = None):
        """定时器快捷方式"""
        self.timer.after(seconds, payload)

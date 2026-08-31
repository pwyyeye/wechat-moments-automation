from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Protocol

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 30 * 60
_cache_lock = threading.Lock()
_cached_identity: "WeChatIdentity | None" = None
_cached_hwnd: int | None = None
_cached_at = 0.0


@dataclass(frozen=True)
class WeChatIdentity:
    nickname: str
    wechat_id: str | None = None


class OcrBlock(Protocol):
    text: str
    x: int
    y: int
    confidence: float


def parse_profile_identity(blocks: Iterable[OcrBlock]) -> WeChatIdentity | None:
    """Extract the account identity from the small self-profile popover."""
    candidates = sorted(
        (
            block
            for block in blocks
            if block.confidence >= 0.55 and block.text.strip()
        ),
        key=lambda block: (block.y, block.x),
    )
    wechat_id: str | None = None
    account_label_y: int | None = None
    for block in candidates:
        match = re.search(
            r"(?:微信号|WeChat\s*ID)\s*[:：]?\s*([A-Za-z0-9_.@-]+)",
            block.text,
            re.IGNORECASE,
        )
        if match:
            wechat_id = match.group(1)
            account_label_y = block.y
            break

    for block in candidates:
        text = block.text.strip()
        if account_label_y is not None and block.y >= account_label_y:
            continue
        if text in {"微信", "Weixin", "朋友圈", "发消息"}:
            continue
        if re.search(r"微信号|WeChat\s*ID", text, re.IGNORECASE):
            continue
        if 1 < len(text) <= 64:
            return WeChatIdentity(nickname=text, wechat_id=wechat_id)
    return None


def find_wechat_main_window() -> int | None:
    import win32gui

    result: list[int] = []

    def callback(hwnd: int, _: object) -> None:
        try:
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if class_name.startswith("Qt") and title == "微信":
                result.append(hwnd)
            elif class_name == "WeChatMainWndForPC":
                result.append(hwnd)
        except Exception:
            return

    win32gui.EnumWindows(callback, None)
    return result[0] if result else None


def _detect_profile_identity(hwnd: int) -> WeChatIdentity | None:
    import numpy as np
    import win32api
    import win32con
    import win32gui
    from PIL import ImageGrab

    from src.locator.ocr_locator import PaddleOCREngine

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    was_visible = bool(win32gui.IsWindowVisible(hwnd))
    previous_foreground = win32gui.GetForegroundWindow()
    previous_cursor = win32api.GetCursorPos()
    try:
        if not was_visible:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 500 or bottom - top < 400:
            return None

        # The self avatar is a stable item in the narrow left navigation rail.
        win32api.SetCursorPos((left + 55, top + 94))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.45)

        profile = ImageGrab.grab(
            bbox=(left + 90, top + 55, left + 525, top + 210),
        )
        blocks = PaddleOCREngine().recognize(np.array(profile))
        return parse_profile_identity(blocks)
    finally:
        try:
            win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
            win32api.keybd_event(
                win32con.VK_ESCAPE,
                0,
                win32con.KEYEVENTF_KEYUP,
                0,
            )
            win32api.SetCursorPos(previous_cursor)
            if not was_visible:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            if previous_foreground and win32gui.IsWindow(previous_foreground):
                win32gui.SetForegroundWindow(previous_foreground)
        except Exception:
            pass


def get_wechat_identity(*, force: bool = False) -> WeChatIdentity | None:
    """Detect once per WeChat session and then serve heartbeats from cache."""
    global _cached_at, _cached_hwnd, _cached_identity

    hwnd = find_wechat_main_window()
    if hwnd is None:
        return None
    now = time.monotonic()
    if (
        not force
        and _cached_hwnd == hwnd
        and _cached_identity is not None
        and now - _cached_at < _CACHE_SECONDS
    ):
        return _cached_identity

    if not _cache_lock.acquire(blocking=False):
        return _cached_identity
    try:
        now = time.monotonic()
        if (
            not force
            and _cached_hwnd == hwnd
            and _cached_identity is not None
            and now - _cached_at < _CACHE_SECONDS
        ):
            return _cached_identity
        try:
            detected = _detect_profile_identity(hwnd)
        except Exception:
            logger.exception("读取微信账号昵称失败")
            detected = None
        _cached_hwnd = hwnd
        _cached_at = now
        if detected is not None:
            _cached_identity = detected
        return _cached_identity
    finally:
        _cache_lock.release()

from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 30 * 60
_RETRY_SECONDS = 60
_cache_lock = threading.Lock()
_cached_identity: "WeChatIdentity | None" = None
_cached_hwnd: int | None = None
_cached_at = 0.0
_identity_state: dict[str, str | None] = {
    "state": "idle",
    "code": None,
    "message": "尚未开始识别",
    "lastAttemptAt": None,
}


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
    account_label: OcrBlock | None = None
    for index, block in enumerate(candidates):
        match = re.search(
            r"(?:微信号|WeChat\s*ID)\s*[:：]?\s*([A-Za-z0-9_.@-]+)",
            block.text,
            re.IGNORECASE,
        )
        if match:
            wechat_id = match.group(1)
            account_label = block
            break
        if re.search(r"微信号|WeChat\s*ID", block.text, re.IGNORECASE):
            account_label = block
            for adjacent in candidates[index + 1 :]:
                if abs(adjacent.y - block.y) > 32:
                    break
                adjacent_text = adjacent.text.strip().lstrip(":：")
                if re.fullmatch(r"[A-Za-z0-9_.@-]+", adjacent_text):
                    wechat_id = adjacent_text
                    break
            break

    if account_label is None:
        return None

    nickname_candidates: list[OcrBlock] = []
    for block in candidates:
        text = block.text.strip()
        if account_label is not None and block.y >= account_label.y:
            continue
        if text in {"微信", "Weixin", "朋友圈", "发消息"}:
            continue
        if re.search(r"微信号|WeChat\s*ID", text, re.IGNORECASE):
            continue
        if 1 < len(text) <= 64:
            nickname_candidates.append(block)
    if not nickname_candidates:
        return None
    if account_label is not None:
        nickname_candidates.sort(
            key=lambda block: (
                abs(account_label.y - block.y),
                abs(account_label.x - block.x),
            )
        )
    return WeChatIdentity(
        nickname=nickname_candidates[0].text.strip(),
        wechat_id=wechat_id,
    )


def _set_identity_state(state: str, code: str | None, message: str) -> None:
    with _cache_lock:
        _identity_state.update(
            {
                "state": state,
                "code": code,
                "message": message,
                "lastAttemptAt": datetime.now(timezone.utc).isoformat(),
            }
        )


def get_wechat_identity_status() -> dict[str, str | None]:
    with _cache_lock:
        return dict(_identity_state)


def _window_process_name(hwnd: int) -> str:
    import win32api
    import win32con
    import win32process

    handle = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False,
            pid,
        )
        path = win32process.GetModuleFileNameEx(handle, 0)
        return Path(path).name.lower()
    except Exception:
        return ""
    finally:
        if handle:
            win32api.CloseHandle(handle)
    return None


def find_wechat_main_window() -> int | None:
    import win32gui

    candidates: list[tuple[int, int]] = []

    def callback(hwnd: int, _: object) -> None:
        try:
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd).strip().lower()
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width < 420 or height < 350:
                return
            process_name = _window_process_name(hwnd)
            is_wechat_process = process_name in {"weixin.exe", "wechat.exe"}
            is_known_title = title in {"微信", "weixin", "wechat"}
            is_known_class = class_name == "WeChatMainWndForPC"
            if not (is_known_class or (class_name.startswith("Qt") and is_known_title) or is_wechat_process):
                return
            score = width * height
            if is_known_class:
                score += 100_000_000
            if is_known_title:
                score += 50_000_000
            if is_wechat_process:
                score += 20_000_000
            if win32gui.IsWindowVisible(hwnd):
                score += 5_000_000
            candidates.append((score, hwnd))
        except Exception:
            return

    win32gui.EnumWindows(callback, None)
    return max(candidates)[1] if candidates else None


def _try_activate_window(hwnd: int) -> bool:
    """Best-effort activation; Windows may reject background focus stealing."""
    import win32api
    import win32con
    import win32gui

    if win32gui.GetForegroundWindow() == hwnd:
        return True
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # A synthetic Alt release grants the current interactive process one
        # more activation attempt on Windows 10/11. Clicking the avatar below
        # remains the final fallback and naturally activates the window.
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(
                win32con.VK_MENU,
                0,
                win32con.KEYEVENTF_KEYUP,
                0,
            )
            win32gui.SetForegroundWindow(hwnd)
        except Exception as error:
            logger.info("Windows declined WeChat foreground activation: %s", error)
    return win32gui.GetForegroundWindow() == hwnd


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
        _try_activate_window(hwnd)
        time.sleep(0.3)

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 500 or bottom - top < 400:
            return None

        engine = PaddleOCREngine()
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        avatar_points = [
            (left + 55, top + 94),
            (client_left + 55, client_top + 94),
            (left + 50, top + 82),
            (client_left + 50, client_top + 82),
            (left + 55, top + 110),
            (left + 45, top + 66),
        ]
        for avatar_screen_x, avatar_screen_y in dict.fromkeys(avatar_points):
            win32api.SetCursorPos((avatar_screen_x, avatar_screen_y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.5)

            profile = ImageGrab.grab(
                bbox=(
                    left + 70,
                    top + 20,
                    min(right, left + 650),
                    min(bottom, top + 320),
                ),
                all_screens=True,
            )
            identity = parse_profile_identity(engine.recognize(np.array(profile)))
            if identity is not None:
                return identity
            win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
            win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.15)
        return None
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
        _set_identity_state(
            "waiting",
            "WECHAT_MAIN_WINDOW_NOT_FOUND",
            "未找到微信主窗口，请确认微信已登录并显示过主界面",
        )
        return None
    now = time.monotonic()
    cache_seconds = _CACHE_SECONDS if _cached_identity is not None else _RETRY_SECONDS
    if (
        not force
        and _cached_hwnd == hwnd
        and now - _cached_at < cache_seconds
    ):
        return _cached_identity

    if not _cache_lock.acquire(blocking=False):
        return _cached_identity
    try:
        _identity_state.update(
            {
                "state": "detecting",
                "code": None,
                "message": "正在读取微信资料卡",
                "lastAttemptAt": datetime.now(timezone.utc).isoformat(),
            }
        )
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
        except Exception as error:
            logger.exception("读取微信账号昵称失败")
            detected = None
            _identity_state.update(
                {
                    "state": "failed",
                    "code": "WECHAT_IDENTITY_READ_FAILED",
                    "message": f"读取微信资料卡失败：{error}",
                }
            )
        _cached_hwnd = hwnd
        _cached_at = now
        if detected is not None:
            _cached_identity = detected
            _identity_state.update(
                {
                    "state": "identified",
                    "code": None,
                    "message": "微信账号识别成功",
                }
            )
        elif _identity_state["state"] != "failed":
            _identity_state.update(
                {
                    "state": "waiting",
                    "code": "WECHAT_PROFILE_NOT_RECOGNIZED",
                    "message": "未识别到资料卡文字，请保持微信已登录并点击“重新识别”",
                }
            )
        return _cached_identity
    finally:
        _cache_lock.release()

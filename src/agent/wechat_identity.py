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

_WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}
_cache_lock = threading.Lock()
_cached_identity: "WeChatIdentity | None" = None
_cached_hwnd: int | None = None
_cached_at = 0.0
_identity_ocr_engine = None
_identity_state: dict[str, str | None] = {
    "state": "idle",
    "code": None,
    "message": "尚未识别，请点击“重新识别”",
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


def get_cached_wechat_identity() -> WeChatIdentity | None:
    """Return the last identity without activating WeChat or running OCR."""
    global _cached_at, _cached_hwnd, _cached_identity

    try:
        current_hwnd = find_wechat_main_window()
    except Exception:
        current_hwnd = None
    with _cache_lock:
        if _cached_identity is not None and current_hwnd != _cached_hwnd:
            _cached_identity = None
            _cached_hwnd = current_hwnd
            _cached_at = 0.0
            _identity_state.update(
                {
                    "state": "waiting",
                    "code": "WECHAT_SESSION_CHANGED",
                    "message": "微信窗口已变化，请点击“重新识别”确认当前账号",
                }
            )
        return _cached_identity


def _get_identity_ocr_engine():
    """Reuse one OCR runtime for explicit identity checks in this process."""
    global _identity_ocr_engine
    if _identity_ocr_engine is None:
        from src.locator.ocr_locator import PaddleOCREngine

        _identity_ocr_engine = PaddleOCREngine()
    return _identity_ocr_engine


def _looks_like_profile(blocks: Iterable[OcrBlock]) -> bool:
    return any(
        re.search(r"微信号|WeChat\s*ID", block.text, re.IGNORECASE)
        for block in blocks
    )


def _window_process_name(hwnd: int) -> str:
    import win32process

    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""

        # PROCESS_QUERY_LIMITED_INFORMATION works across integrity levels where
        # GetModuleFileNameEx(PROCESS_VM_READ) is commonly denied.
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = ctypes.c_ulong(len(buffer))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return Path(buffer.value).name.lower()
            finally:
                kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _is_wechat_main_candidate(
    class_name: str,
    title: str,
    process_name: str,
    width: int,
    height: int,
    iconic: bool,
) -> tuple[bool, int]:
    """Classify a top-level window without relying on a WeChat version string."""
    normalized_title = title.strip().lower()
    is_known_title = normalized_title in {"微信", "weixin", "wechat"}
    is_known_class = class_name == "WeChatMainWndForPC"
    is_qt_main = class_name.startswith("Qt") and is_known_title
    is_wechat_process = process_name in _WECHAT_PROCESS_NAMES

    # Minimized windows can report a tiny placeholder rectangle. A known main
    # class/title remains valid and will be restored before interaction.
    known_shape = iconic or (width >= 120 and height >= 80)
    process_shape = iconic or (width >= 240 and height >= 160)
    if not ((is_known_class or is_qt_main) and known_shape) and not (
        is_wechat_process and process_shape
    ):
        return False, 0

    area = max(width, 0) * max(height, 0)
    score = area
    if is_known_class:
        score += 100_000_000
    if is_known_title:
        score += 50_000_000
    if is_wechat_process:
        score += 20_000_000
    if not iconic:
        score += 1_000_000
    return True, score


def find_wechat_main_window() -> int | None:
    import win32gui

    candidates: list[tuple[int, int]] = []

    def callback(hwnd: int, _: object) -> None:
        try:
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            process_name = _window_process_name(hwnd)
            accepted, score = _is_wechat_main_candidate(
                class_name,
                title,
                process_name,
                width,
                height,
                bool(win32gui.IsIconic(hwnd)),
            )
            if not accepted:
                return
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


def _profile_avatar_points(
    *,
    left: int,
    top: int,
    client_left: int,
    client_top: int,
    scale: float,
) -> list[tuple[int, int]]:
    """Return a finite set of known-safe avatar points across WeChat layouts."""
    candidates = [
        (
            client_left + round(38 * scale),
            client_top + round(60 * scale),
        ),
        (client_left + 55, client_top + 94),
        (left + 55, top + 94),
    ]
    return list(dict.fromkeys(candidates))


def _detect_profile_identity(hwnd: int) -> WeChatIdentity | None:
    import numpy as np
    import win32api
    import win32con
    import win32gui
    from PIL import ImageGrab

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    was_visible = bool(win32gui.IsWindowVisible(hwnd))
    previous_foreground = win32gui.GetForegroundWindow()
    previous_cursor = win32api.GetCursorPos()
    profile_opened = False
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

        engine = _get_identity_ocr_engine()
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
        dpi = get_dpi(hwnd) if get_dpi else 96
        scale = min(max((dpi or 96) / 96, 0.75), 3.0)
        avatar_points = _profile_avatar_points(
            left=left,
            top=top,
            client_left=client_left,
            client_top=client_top,
            scale=scale,
        )
        logger.info(
            "manual WeChat identity detection started hwnd=%s points=%s dpi=%s",
            hwnd,
            avatar_points,
            dpi,
        )
        for attempt, (avatar_screen_x, avatar_screen_y) in enumerate(avatar_points, 1):
            if not (left <= avatar_screen_x < right and top <= avatar_screen_y < bottom):
                continue
            win32api.SetCursorPos((avatar_screen_x, avatar_screen_y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.08)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.65)

            profile = ImageGrab.grab(
                bbox=(
                    client_left + round(42 * scale),
                    client_top,
                    min(right, client_left + round(480 * scale)),
                    min(bottom, client_top + round(320 * scale)),
                ),
                all_screens=True,
            )
            blocks = engine.recognize(np.array(profile))
            identity = parse_profile_identity(blocks)
            profile_opened = identity is not None or _looks_like_profile(blocks)
            logger.info(
                "manual WeChat identity attempt=%s point=(%s,%s) blocks=%s recognized=%s profileOpened=%s",
                attempt,
                avatar_screen_x,
                avatar_screen_y,
                len(blocks),
                identity is not None,
                profile_opened,
            )
            if identity is not None or profile_opened:
                return identity

        logger.info(
            "manual WeChat identity detection finished hwnd=%s recognized=false profileOpened=false",
            hwnd,
        )
        return None
    finally:
        try:
            if profile_opened:
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
    """Return the cache unless a caller explicitly requests one UI interaction."""
    global _cached_at, _cached_hwnd, _cached_identity

    if not force:
        return get_cached_wechat_identity()

    hwnd = find_wechat_main_window()
    if hwnd is None:
        with _cache_lock:
            _cached_identity = None
            _cached_hwnd = None
            _cached_at = 0.0
        _set_identity_state(
            "waiting",
            "WECHAT_MAIN_WINDOW_NOT_FOUND",
            "未找到微信主窗口，请确认微信已登录并显示过主界面",
        )
        return None
    now = time.monotonic()

    if not _cache_lock.acquire(blocking=False):
        return None
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
        else:
            _cached_identity = None
            if _identity_state["state"] != "failed":
                _identity_state.update(
                    {
                        "state": "waiting",
                        "code": "WECHAT_PROFILE_NOT_RECOGNIZED",
                        "message": "未识别到资料卡文字，请保持微信已登录并点击“重新识别”",
                    }
                )
        return detected
    finally:
        _cache_lock.release()

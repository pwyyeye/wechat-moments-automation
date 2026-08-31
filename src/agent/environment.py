from __future__ import annotations

import ctypes
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .models import AgentSnapshot

logger = logging.getLogger(__name__)


def _moments_window_ready() -> bool:
    try:
        import win32gui

        windows: list[tuple[int, str]] = []
        win32gui.EnumWindows(
            lambda hwnd, result: result.append((hwnd, win32gui.GetWindowText(hwnd)))
            if win32gui.IsWindowVisible(hwnd)
            else None,
            windows,
        )
        return any(title == "朋友圈" for _, title in windows)
    except Exception:
        return False


def _locate_moments_icon(
    screen: Any,
    template: Any,
    threshold: float = 0.78,
) -> tuple[int, int, float] | None:
    """Locate the distinctive Moments icon inside a pre-cropped nav region."""
    import cv2

    screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    screen_edges = cv2.Canny(screen_gray, 50, 150)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    best: tuple[float, tuple[int, int], int, int, Any] | None = None
    for scale in (0.75, 0.85, 1.0, 1.15, 1.25, 1.5, 1.75, 2.0):
        width = max(12, int(template_gray.shape[1] * scale))
        height = max(12, int(template_gray.shape[0] * scale))
        if width >= screen_gray.shape[1] or height >= screen_gray.shape[0]:
            continue
        resized = cv2.resize(template_gray, (width, height), interpolation=cv2.INTER_AREA)
        edges = cv2.Canny(resized, 50, 150)
        scores = cv2.matchTemplate(screen_edges, edges, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        if best is None or score > best[0]:
            best = score, location, width, height, scores

    if best is None or best[0] < threshold:
        return None

    score, (left, top), width, height, scores = best
    competing = scores.copy()
    competing[
        max(0, top - height):min(competing.shape[0], top + height),
        max(0, left - width):min(competing.shape[1], left + width),
    ] = -1
    second_score = cv2.minMaxLoc(competing)[1]
    if score - second_score < 0.05:
        logger.warning("朋友圈图标匹配不唯一: %.3f / %.3f", score, second_score)
        return None
    return left + width // 2, top + height // 2, float(score)


def _open_moments_by_template() -> bool:
    """Click only a high-confidence Moments icon in the left navigation area."""
    import cv2
    import numpy as np
    import win32api
    import win32con
    import win32gui
    from PIL import ImageGrab
    from src.executor.wechat_discovery import _find_wechat_windows

    windows = _find_wechat_windows()
    if not windows:
        return False
    hwnd = windows[0][0]
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
    )
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
    )

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right - left < 300 or bottom - top < 300:
        return False
    screenshot = np.array(ImageGrab.grab(bbox=(left, top, right, bottom)))
    screenshot = cv2.cvtColor(screenshot, cv2.COLOR_RGB2BGR)
    nav_region = screenshot[:int(screenshot.shape[0] * 0.82), :int(screenshot.shape[1] * 0.25)]

    template_path = Path(__file__).resolve().parents[2] / "templates" / "icons" / "moments_tab.png"
    template = cv2.imread(str(template_path))
    if template is None:
        logger.error("朋友圈图标模板缺失: %s", template_path)
        return False
    match = _locate_moments_icon(nav_region, template)
    if match is None:
        logger.error("未找到高置信度朋友圈图标，拒绝点击")
        return False

    x, y, score = match
    win32api.SetCursorPos((left + x, top + y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    logger.info("已点击受约束朋友圈图标，匹配置信度 %.3f", score)
    return True


def _wait_for_moments_window(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _moments_window_ready():
            return True
        time.sleep(0.25)
    return False


def prepare_moments_window(timeout: float = 10.0) -> bool:
    """Open the Moments list without initializing OCR or entering compose."""
    if not is_interactive_session() or not is_desktop_unlocked():
        return False
    if _moments_window_ready():
        return True

    from src.executor.uia_bridge import UIABridge

    bridge = UIABridge()
    if bridge.available and bridge.open_moments():
        if _wait_for_moments_window(min(timeout, 3.0)):
            return True

    if not _open_moments_by_template():
        return False
    return _wait_for_moments_window(timeout)


def is_interactive_session() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetShellWindow())
    except Exception:
        return False


def is_desktop_unlocked() -> bool:
    if sys.platform != "win32":
        return False
    desktop = None
    try:
        # SwitchDesktop is denied for the Winlogon secure desktop and remains
        # reliable when an unlocked desktop temporarily has no foreground HWND.
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.SwitchDesktop.argtypes = [ctypes.c_void_p]
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        return bool(desktop and user32.SwitchDesktop(desktop))
    except Exception:
        return False
    finally:
        if desktop:
            ctypes.windll.user32.CloseDesktop(desktop)


def probe_environment() -> AgentSnapshot:
    running = False
    logged_in = False
    moments_ready = False
    version = "unknown"
    nickname = None
    wechat_id = None
    try:
        from src.core.account_manager import WeChatWindowFinder

        from .wechat_identity import find_wechat_main_window

        running = bool(WeChatWindowFinder.enum_all()) or find_wechat_main_window() is not None
    except Exception:
        running = False
    if running:
        # The full publisher performs semantic login checks before execution.
        # Heartbeats deliberately stay read-only and avoid activating windows.
        logged_in = True
        if is_interactive_session() and is_desktop_unlocked():
            try:
                from .wechat_identity import get_wechat_identity

                identity = get_wechat_identity()
                if identity:
                    nickname = identity.nickname
                    wechat_id = identity.wechat_id
            except Exception:
                logger.exception("微信账号身份检测失败")
    try:
        from src.executor.version_detector import VersionDetector

        detected = VersionDetector().get_version()
        if detected:
            version = detected.raw
    except Exception:
        pass
    moments_ready = _moments_window_ready()
    return AgentSnapshot(
        running=running,
        loggedIn=logged_in,
        momentsWindowReady=moments_ready,
        wechatVersion=version,
        wechatNickname=nickname,
        wechatId=wechat_id,
        interactiveSession=is_interactive_session(),
        desktopUnlocked=is_desktop_unlocked(),
    )

from __future__ import annotations

import ctypes
import sys

from .models import AgentSnapshot


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
    try:
        # A non-zero foreground window is a conservative signal that the input
        # desktop is available to this user session.
        return bool(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return False


def probe_environment() -> AgentSnapshot:
    running = False
    logged_in = False
    moments_ready = False
    version = "unknown"
    try:
        from src.core.account_manager import WeChatWindowFinder

        running = bool(WeChatWindowFinder.enum_all())
    except Exception:
        running = False
    if running:
        # The full publisher performs semantic login checks before execution.
        # Heartbeats deliberately stay read-only and avoid activating windows.
        logged_in = True
    try:
        from src.executor.version_detector import VersionDetector

        detected = VersionDetector().get_version()
        if detected:
            version = detected.raw
    except Exception:
        pass
    try:
        import win32gui

        windows: list[tuple[int, str]] = []
        win32gui.EnumWindows(
            lambda hwnd, result: result.append((hwnd, win32gui.GetWindowText(hwnd)))
            if win32gui.IsWindowVisible(hwnd)
            else None,
            windows,
        )
        moments_ready = any(title == "朋友圈" for _, title in windows)
    except Exception:
        moments_ready = False
    return AgentSnapshot(
        running=running,
        loggedIn=logged_in,
        momentsWindowReady=moments_ready,
        wechatVersion=version,
        interactiveSession=is_interactive_session(),
        desktopUnlocked=is_desktop_unlocked(),
    )

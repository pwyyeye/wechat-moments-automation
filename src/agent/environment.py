from __future__ import annotations

import ctypes
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import AgentSnapshot

logger = logging.getLogger(__name__)


def _get_window_text(hwnd: int) -> str:
    """Read a title through the Unicode API even on legacy system code pages."""
    try:
        buffer = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value
    except Exception:
        return ""


def _moments_window_ready() -> bool:
    try:
        import win32gui
        import win32process

        from src.executor.wechat_discovery import WECHAT_PROCESS_NAMES, _get_process_name

        windows: list[int] = []
        win32gui.EnumWindows(
            lambda hwnd, result: result.append(hwnd)
            if win32gui.IsWindowVisible(hwnd)
            else None,
            windows,
        )
        for hwnd in windows:
            if _get_window_text(hwnd) != "朋友圈":
                continue
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if _get_process_name(pid) in WECHAT_PROCESS_NAMES:
                return True
        return False
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
    for scale in (0.3, 0.4, 0.5, 0.6, 0.75, 0.85, 1.0, 1.15, 1.25, 1.5, 1.75, 2.0):
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


@contextmanager
def _per_monitor_dpi_context():
    """Keep screenshot pixels and cursor coordinates aligned on scaled displays."""
    previous = None
    try:
        previous = ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    try:
        yield
    finally:
        if previous:
            try:
                ctypes.windll.user32.SetThreadDpiAwarenessContext(previous)
            except Exception:
                pass


def _open_moments_by_template() -> bool:
    """Open Moments visually through a direct tab or Discover -> Moments."""
    import cv2
    import numpy as np
    import win32api
    import win32con
    import win32gui
    from PIL import ImageGrab
    from .wechat_identity import find_wechat_main_window, _try_activate_window

    hwnd = find_wechat_main_window()
    if hwnd is None:
        return False
    if not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
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
    _try_activate_window(hwnd)
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

    templates_dir = Path(__file__).resolve().parents[2] / "templates" / "icons"
    moments_path = templates_dir / "moments_tab.png"
    nested_moments_path = templates_dir / "moments_discover_item.png"
    discover_path = templates_dir / "discover_tab.png"
    moments_template = cv2.imread(str(moments_path))
    nested_moments_template = cv2.imread(str(nested_moments_path))
    discover_template = cv2.imread(str(discover_path))
    if (
        moments_template is None
        or nested_moments_template is None
        or discover_template is None
    ):
        logger.error(
            "微信导航图标模板缺失: %s / %s / %s",
            moments_path,
            nested_moments_path,
            discover_path,
        )
        return False

    def click_match(left: int, top: int, match: tuple[int, int, float]) -> None:
        x, y, _ = match
        win32api.SetCursorPos((left + x, top + y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    with _per_monitor_dpi_context():
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 300 or bottom - top < 300:
            return False

        def capture() -> Any:
            image = np.array(
                ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            )
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

        screenshot = capture()
        rail = screenshot[
            :int(screenshot.shape[0] * 0.82),
            :max(80, int(screenshot.shape[1] * 0.12)),
        ]
        direct_match = _locate_moments_icon(rail, moments_template)
        if direct_match is not None:
            click_match(left, top, direct_match)
            logger.info("已通过直接入口打开朋友圈，匹配置信度 %.3f", direct_match[2])
            return True

        initial_panel = screenshot[
            :int(screenshot.shape[0] * 0.48),
            :max(180, int(screenshot.shape[1] * 0.32)),
        ]
        nested_match = _locate_moments_icon(
            initial_panel,
            nested_moments_template,
        )
        if nested_match is not None:
            click_match(left, top, nested_match)
            logger.info(
                "已从当前发现页打开朋友圈，匹配置信度 %.3f",
                nested_match[2],
            )
            return True

        discover_match = _locate_moments_icon(rail, discover_template)
        if discover_match is None:
            logger.error("未找到高置信度的朋友圈或发现图标，拒绝点击")
            return False
        click_match(left, top, discover_match)
        logger.info("已打开微信发现页，匹配置信度 %.3f", discover_match[2])

        # The submenu is rendered asynchronously and may shift with DPI or
        # window size, so locate its semantic icon instead of using a point.
        for _ in range(8):
            time.sleep(0.25)
            screenshot = capture()
            discover_panel = screenshot[
                :int(screenshot.shape[0] * 0.48),
                :max(180, int(screenshot.shape[1] * 0.32)),
            ]
            nested_match = _locate_moments_icon(
                discover_panel,
                nested_moments_template,
            )
            if nested_match is None:
                continue
            click_match(left, top, nested_match)
            logger.info(
                "已通过发现页打开朋友圈，匹配置信度 %.3f",
                nested_match[2],
            )
            return True

    logger.error("发现页已打开，但未找到高置信度朋友圈图标")
    return False


def _wait_for_moments_window(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _moments_window_ready():
            return True
        time.sleep(0.25)
    return False


def prepare_moments_window(timeout: float = 10.0) -> bool:
    """Open the Moments list without initializing OCR or entering compose."""
    deadline = time.monotonic() + max(1.0, timeout)
    if not is_interactive_session() or not is_desktop_unlocked():
        return False
    if _moments_window_ready():
        return True

    from src.executor.uia_bridge import UIABridge

    bridge = UIABridge()
    remaining = max(0.1, deadline - time.monotonic())
    if bridge.available and bridge.open_moments(timeout=min(8.0, remaining)):
        if _wait_for_moments_window(min(3.0, max(0.1, deadline - time.monotonic()))):
            return True

    if time.monotonic() >= deadline:
        return False
    if not _open_moments_by_template():
        return False
    return _wait_for_moments_window(max(0.1, deadline - time.monotonic()))


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
        # Status and heartbeat probes must never activate windows or initialize
        # OCR. Identity detection runs independently and only exposes its cache.
        logged_in = True
        try:
            from .wechat_identity import get_cached_wechat_identity

            identity = get_cached_wechat_identity()
            if identity:
                nickname = identity.nickname
                wechat_id = identity.wechat_id
        except Exception:
            logger.exception("读取微信账号身份缓存失败")
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

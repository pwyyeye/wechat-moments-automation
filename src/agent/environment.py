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
    *,
    require_unique: bool = True,
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
    if require_unique and score - second_score < 0.05:
        logger.warning("朋友圈图标匹配不唯一: %.3f / %.3f", score, second_score)
        return None
    return left + width // 2, top + height // 2, float(score)


def _locate_icon_candidates(
    screen: Any,
    template: Any,
    threshold: float = 0.30,
    limit: int = 5,
) -> list[tuple[int, int, float]]:
    """Return distinct visual candidates that can be verified by a tooltip."""
    import cv2

    screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    screen_edges = cv2.Canny(screen_gray, 50, 150)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    raw: list[tuple[int, int, float]] = []
    for scale in (0.3, 0.4, 0.5, 0.6, 0.75, 0.85, 1.0, 1.15, 1.25, 1.5, 1.75, 2.0):
        width = max(12, int(template_gray.shape[1] * scale))
        height = max(12, int(template_gray.shape[0] * scale))
        if width >= screen_gray.shape[1] or height >= screen_gray.shape[0]:
            continue
        resized = cv2.resize(template_gray, (width, height), interpolation=cv2.INTER_AREA)
        edges = cv2.Canny(resized, 50, 150)
        scores = cv2.matchTemplate(screen_edges, edges, cv2.TM_CCOEFF_NORMED)
        for _ in range(3):
            _, score, _, location = cv2.minMaxLoc(scores)
            if score < threshold:
                break
            left, top = location
            raw.append((left + width // 2, top + height // 2, float(score)))
            scores[
                max(0, top - height):min(scores.shape[0], top + height),
                max(0, left - width):min(scores.shape[1], left + width),
            ] = -1

    distinct: list[tuple[int, int, float]] = []
    for candidate in sorted(raw, reverse=True, key=lambda item: item[2]):
        x, y, _ = candidate
        if any(abs(x - seen_x) < 18 and abs(y - seen_y) < 18 for seen_x, seen_y, _ in distinct):
            continue
        distinct.append(candidate)
        if len(distinct) >= limit:
            break
    return distinct


def _classify_navigation_candidate(
    template_role: str,
    tooltip_label: str | None,
    score: float,
) -> str | None:
    """Prefer the hover tooltip over historical template naming."""
    if tooltip_label == "朋友圈":
        return "moments"
    if tooltip_label == "发现":
        return "discover"
    if score >= 0.78 and template_role in {"moments", "discover"}:
        return template_role
    return None


def _tooltip_label_from_blocks(blocks: Any) -> str | None:
    """Extract a supported navigation label from OCR blocks near the cursor."""
    for block in blocks:
        if getattr(block, "confidence", 0.0) < 0.5:
            continue
        text = "".join(str(getattr(block, "text", "")).split())
        if "朋友圈" in text:
            return "朋友圈"
        if text == "发现" or text.endswith("发现"):
            return "发现"
    return None


def _discover_moments_match_from_blocks(
    blocks: Any,
    *,
    panel_width: int,
    panel_height: int,
    nav_width: int,
) -> tuple[int, int, float] | None:
    """Locate the semantic Moments row only inside a confirmed Discover menu."""
    if panel_width <= 0 or panel_height <= 0:
        return None

    content_left = nav_width + max(8, nav_width // 12)
    content_right = max(content_left + 1, int(panel_width * 0.92))
    content_bottom = max(1, int(panel_height * 0.92))
    visible: list[tuple[Any, str]] = []
    for block in blocks:
        if getattr(block, "confidence", 0.0) < 0.5:
            continue
        text = "".join(str(getattr(block, "text", "")).split())
        x = int(getattr(block, "x", -1))
        y = int(getattr(block, "y", -1))
        if not (content_left <= x <= content_right and 0 <= y <= content_bottom):
            continue
        visible.append((block, text))

    # A standalone "朋友圈" can appear in chat text. Requiring two other
    # Discover entries prevents the fallback from clicking unrelated content.
    discover_labels = {"发现", "视频号", "搜一搜", "游戏", "小程序"}
    context_labels = {text for _, text in visible if text in discover_labels}
    if len(context_labels) < 2:
        return None

    candidates = [
        block
        for block, text in visible
        if text == "朋友圈" and getattr(block, "confidence", 0.0) >= 0.55
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda block: getattr(block, "confidence", 0.0))
    return int(best.x), int(best.y), float(best.confidence)


def _locate_discover_moments_text(
    panel: Any,
    *,
    nav_width: int,
) -> tuple[int, int, float] | None:
    """Use bundled Chinese OCR when the Discover row icon has changed."""
    from .wechat_identity import _get_identity_ocr_engine

    try:
        blocks = _get_identity_ocr_engine().recognize(panel)
        match = _discover_moments_match_from_blocks(
            blocks,
            panel_width=int(panel.shape[1]),
            panel_height=int(panel.shape[0]),
            nav_width=nav_width,
        )
        logger.info(
            "发现页 OCR 定位朋友圈: match=%s texts=%s",
            match or "none",
            [str(getattr(block, "text", ""))[:24] for block in blocks[:12]],
        )
        return match
    except Exception as error:
        logger.warning("发现页 OCR 定位朋友圈失败: %s", error)
        return None


def _read_hover_tooltip(
    left: int,
    top: int,
    right: int,
    bottom: int,
    match: tuple[int, int, float],
    baseline: Any | None = None,
) -> str | None:
    """Read the Qt-rendered navigation tooltip after hovering a candidate."""
    import numpy as np
    from PIL import ImageGrab

    from .wechat_identity import _get_identity_ocr_engine

    x, y, _ = match
    time.sleep(1.0)
    tooltip_box = (
        max(left, left + x - 24),
        max(top, top + y - 72),
        min(right, left + x + 260),
        min(bottom, top + y + 96),
    )
    if tooltip_box[2] <= tooltip_box[0] or tooltip_box[3] <= tooltip_box[1]:
        return None
    try:
        image = np.array(ImageGrab.grab(bbox=tooltip_box, all_screens=True))
        engine = _get_identity_ocr_engine()
        blocks = engine.recognize(image)
        label = _tooltip_label_from_blocks(blocks)
        if label and baseline is not None:
            crop_left = tooltip_box[0] - left
            crop_top = tooltip_box[1] - top
            crop_right = tooltip_box[2] - left
            crop_bottom = tooltip_box[3] - top
            before_blocks = engine.recognize(
                baseline[crop_top:crop_bottom, crop_left:crop_right]
            )
            if _tooltip_label_from_blocks(before_blocks) == label:
                logger.info("微信导航 Tooltip 文本在悬停前已存在，拒绝作为语义确认: %s", label)
                label = None
        logger.info(
            "微信导航 Tooltip 识别: label=%s texts=%s",
            label or "unknown",
            [str(getattr(block, "text", ""))[:32] for block in blocks[:8]],
        )
        return label
    except Exception as error:
        logger.warning("微信导航 Tooltip 识别失败: %s", error)
        return None


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

    from .wechat_identity import _try_activate_window, find_wechat_main_window

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

        win32api.SetCursorPos(
            (min(right - 1, left + 300), min(bottom - 1, top + 24))
        )
        time.sleep(0.1)
        screenshot = capture()
        get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
        dpi = get_dpi(hwnd) if get_dpi else 96
        nav_width = min(
            screenshot.shape[1],
            max(80, round(74 * min(max((dpi or 96) / 96, 0.75), 3.0))),
        )
        rail = screenshot[
            :int(screenshot.shape[0] * 0.82),
            :nav_width,
        ]
        initial_panel = screenshot[
            :int(screenshot.shape[0] * 0.48),
            :max(180, int(screenshot.shape[1] * 0.32)),
        ]
        nested_match = _locate_moments_icon(
            initial_panel,
            nested_moments_template,
        )
        nested_method = "icon-template"
        if nested_match is None:
            nested_match = _locate_discover_moments_text(
                initial_panel,
                nav_width=nav_width,
            )
            nested_method = "ocr-text"
        if nested_match is not None:
            click_match(left, top, nested_match)
            logger.info(
                "已从当前发现页打开朋友圈，method=%s confidence=%.3f",
                nested_method,
                nested_match[2],
            )
            return _wait_for_moments_window(3.0)

        # WeChat 4.x renders navigation as an unnamed canvas. Historical icon
        # templates can also swap visual meaning between releases, so hover the
        # strongest candidates and let the Chinese tooltip decide the action.
        candidates: list[tuple[float, str, tuple[int, int, float]]] = []
        for template_role, template in (
            ("moments", moments_template),
            ("discover", discover_template),
        ):
            matches = _locate_icon_candidates(
                rail,
                template,
                threshold=0.30,
                limit=5,
            )
            for match in matches:
                candidates.append((match[2], template_role, match))
        candidates.sort(reverse=True, key=lambda item: item[0])

        navigation: tuple[str, tuple[int, int, float], str | None] | None = None
        visited: list[tuple[int, int]] = []
        for score, template_role, match in candidates[:8]:
            x, y, _ = match
            if any(abs(x - seen_x) < 14 and abs(y - seen_y) < 14 for seen_x, seen_y in visited):
                continue
            visited.append((x, y))
            # Moving away first retriggers Qt's hover timer when the cursor was
            # already left on the same icon by a previous preflight.
            win32api.SetCursorPos(
                (
                    min(right - 1, left + nav_width + 24),
                    min(bottom - 1, max(top, top + y)),
                )
            )
            time.sleep(0.08)
            win32api.SetCursorPos((left + x, top + y))
            tooltip_label = _read_hover_tooltip(
                left,
                top,
                right,
                bottom,
                match,
                screenshot,
            )
            action = _classify_navigation_candidate(template_role, tooltip_label, score)
            logger.info(
                "微信导航候选: template=%s score=%.3f tooltip=%s action=%s point=(%s,%s)",
                template_role,
                score,
                tooltip_label or "unknown",
                action or "rejected",
                x,
                y,
            )
            if action is not None:
                navigation = action, match, tooltip_label
                break

        if navigation is None:
            logger.error("未找到高置信度的朋友圈或发现图标，拒绝点击")
            return False

        action, navigation_match, tooltip_label = navigation
        click_match(left, top, navigation_match)

        # The icon previously named discover_tab.png is a direct Moments entry
        # in some WeChat 4.x builds. Always check the resulting window before
        # assuming that a nested Discover page must be traversed.
        if _wait_for_moments_window(3.0):
            logger.info(
                "已打开朋友圈: tooltip=%s template=%s score=%.3f",
                tooltip_label or "unknown",
                action,
                navigation_match[2],
            )
            return True
        if action == "moments":
            logger.error("Tooltip 已确认朋友圈，但点击后未检测到朋友圈窗口")
            return False

        logger.info(
            "已打开微信发现页: tooltip=%s score=%.3f",
            tooltip_label or "unknown",
            navigation_match[2],
        )

        # The submenu is rendered asynchronously and may shift with DPI or
        # window size, so locate its semantic icon instead of using a point.
        for attempt in range(8):
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
            nested_method = "icon-template"
            if nested_match is None and attempt in {1, 5}:
                nested_match = _locate_discover_moments_text(
                    discover_panel,
                    nav_width=nav_width,
                )
                nested_method = "ocr-text"
            if nested_match is None:
                continue
            click_match(left, top, nested_match)
            logger.info(
                "已通过发现页打开朋友圈，method=%s confidence=%.3f",
                nested_method,
                nested_match[2],
            )
            return _wait_for_moments_window(3.0)

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
    """Open the Moments list without entering the compose flow."""
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
    return _open_moments_by_template()


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

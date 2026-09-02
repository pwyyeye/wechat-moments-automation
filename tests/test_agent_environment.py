from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from src.agent.environment import (
    _classify_navigation_candidate,
    _discover_moments_match_from_blocks,
    _locate_icon_candidates,
    _locate_moments_icon,
    _moments_window_ready,
    _tooltip_label_from_blocks,
    prepare_moments_window,
)


def test_locate_moments_icon_accepts_unique_scaled_match() -> None:
    template = np.full((40, 40, 3), 220, dtype=np.uint8)
    cv2.circle(template, (20, 20), 12, (40, 40, 40), 3)
    cv2.line(template, (20, 8), (28, 28), (40, 40, 40), 3)
    scaled = cv2.resize(template, (50, 50), interpolation=cv2.INTER_AREA)
    screen = np.full((300, 200, 3), 220, dtype=np.uint8)
    screen[120:170, 60:110] = scaled

    match = _locate_moments_icon(screen, template)

    assert match is not None
    assert abs(match[0] - 85) <= 2
    assert abs(match[1] - 145) <= 2
    assert match[2] >= 0.78


def test_locate_moments_icon_rejects_low_confidence_screen() -> None:
    screen = np.zeros((300, 200, 3), dtype=np.uint8)
    template = np.full((40, 40, 3), 255, dtype=np.uint8)

    assert _locate_moments_icon(screen, template) is None


def test_locate_icon_candidates_returns_distinct_hover_targets() -> None:
    template = np.full((40, 40, 3), 220, dtype=np.uint8)
    cv2.circle(template, (20, 20), 12, (40, 40, 40), 3)
    cv2.line(template, (20, 8), (28, 28), (40, 40, 40), 3)
    screen = np.full((300, 200, 3), 220, dtype=np.uint8)
    screen[50:90, 60:100] = template
    screen[180:220, 60:100] = template

    candidates = _locate_icon_candidates(screen, template, limit=5)

    assert any(abs(x - 80) <= 2 and abs(y - 70) <= 2 for x, y, _ in candidates)
    assert any(abs(x - 80) <= 2 and abs(y - 200) <= 2 for x, y, _ in candidates)


def test_hover_tooltip_overrides_historical_template_role() -> None:
    assert _classify_navigation_candidate("discover", "朋友圈", 0.98) == "moments"
    assert _classify_navigation_candidate("moments", "发现", 0.98) == "discover"


def test_unverified_low_confidence_navigation_candidate_is_rejected() -> None:
    assert _classify_navigation_candidate("discover", None, 0.70) is None


def test_tooltip_label_is_extracted_from_confident_ocr_blocks() -> None:
    blocks = [
        SimpleNamespace(text="通讯录", confidence=0.99),
        SimpleNamespace(text=" 朋 友 圈 ", confidence=0.95),
    ]

    assert _tooltip_label_from_blocks(blocks) == "朋友圈"


def test_discover_moments_text_requires_semantic_menu_context() -> None:
    blocks = [
        SimpleNamespace(text="发现", x=180, y=30, confidence=0.99),
        SimpleNamespace(text="朋友圈", x=220, y=100, confidence=0.98),
        SimpleNamespace(text="视频号", x=220, y=160, confidence=0.97),
        SimpleNamespace(text="搜一搜", x=220, y=220, confidence=0.96),
    ]

    assert _discover_moments_match_from_blocks(
        blocks,
        panel_width=600,
        panel_height=400,
        nav_width=100,
    ) == (220, 100, 0.98)


def test_discover_moments_text_rejects_unrelated_chat_text() -> None:
    blocks = [
        SimpleNamespace(text="朋友圈", x=220, y=100, confidence=0.98),
        SimpleNamespace(text="今天发现一个游戏", x=220, y=160, confidence=0.97),
    ]

    assert _discover_moments_match_from_blocks(
        blocks,
        panel_width=600,
        panel_height=400,
        nav_width=100,
    ) is None


def test_discover_moments_text_rejects_navigation_tooltip() -> None:
    blocks = [
        SimpleNamespace(text="朋友圈", x=80, y=100, confidence=0.98),
        SimpleNamespace(text="发现", x=180, y=30, confidence=0.99),
        SimpleNamespace(text="视频号", x=220, y=160, confidence=0.97),
    ]

    assert _discover_moments_match_from_blocks(
        blocks,
        panel_width=600,
        panel_height=400,
        nav_width=100,
    ) is None


def test_bundled_direct_and_discover_navigation_templates_are_loadable() -> None:
    icons = Path(__file__).parents[1] / "templates" / "icons"

    assert cv2.imread(str(icons / "moments_tab.png")) is not None
    assert cv2.imread(str(icons / "discover_tab.png")) is not None
    assert cv2.imread(str(icons / "moments_discover_item.png")) is not None


def test_moments_window_ready_uses_unicode_title_and_wechat_process() -> None:
    def enum_windows(callback, result) -> None:
        callback(123, result)

    with (
        patch("win32gui.EnumWindows", side_effect=enum_windows),
        patch("win32gui.IsWindowVisible", return_value=True),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 456)),
        patch("src.agent.environment._get_window_text", return_value="朋友圈"),
        patch(
            "src.executor.wechat_discovery._get_process_name",
            return_value="Weixin.exe",
        ),
    ):
        assert _moments_window_ready()


def test_prepare_moments_window_uses_safe_template_fallback() -> None:
    bridge = type(
        "Bridge",
        (),
        {"available": True, "open_moments": lambda self, timeout=None: False},
    )()
    with (
        patch("src.agent.environment.is_interactive_session", return_value=True),
        patch("src.agent.environment.is_desktop_unlocked", return_value=True),
        patch("src.agent.environment._moments_window_ready", return_value=False),
        patch("src.executor.uia_bridge.UIABridge", return_value=bridge),
        patch("src.agent.environment._open_moments_by_template", return_value=True) as fallback,
        patch("src.agent.environment._wait_for_moments_window", return_value=True),
    ):
        assert prepare_moments_window(timeout=1.0)

    fallback.assert_called_once_with()


def test_prepare_moments_window_never_clicks_when_desktop_is_locked() -> None:
    with (
        patch("src.agent.environment.is_interactive_session", return_value=True),
        patch("src.agent.environment.is_desktop_unlocked", return_value=False),
        patch("src.agent.environment._open_moments_by_template") as fallback,
    ):
        assert not prepare_moments_window()

    fallback.assert_not_called()

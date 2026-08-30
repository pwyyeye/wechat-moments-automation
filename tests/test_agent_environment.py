from unittest.mock import patch

import cv2
import numpy as np

from src.agent.environment import _locate_moments_icon, prepare_moments_window


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


def test_prepare_moments_window_uses_safe_template_fallback() -> None:
    bridge = type("Bridge", (), {"available": True, "open_moments": lambda self: False})()
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

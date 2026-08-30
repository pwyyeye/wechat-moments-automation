from unittest.mock import Mock, patch

from src.agent.executor import DesktopPublishExecutor
from src.agent.models import AgentSnapshot


def snapshot(*, ready: bool = False) -> AgentSnapshot:
    return AgentSnapshot(
        running=True,
        loggedIn=True,
        momentsWindowReady=ready,
        wechatVersion="4.1.13.12",
        interactiveSession=True,
        desktopUnlocked=True,
    )


def test_preflight_reports_the_prepared_moments_window() -> None:
    publisher_factory = Mock()
    executor = DesktopPublishExecutor(publisher_factory=publisher_factory)

    with (
        patch("src.agent.executor.probe_environment", side_effect=[snapshot(), snapshot()]),
        patch("src.agent.executor.prepare_moments_window", return_value=True) as prepare,
    ):
        result = executor.preflight()

    assert result.logged_in is True
    assert result.moments_window_ready is True
    prepare.assert_called_once_with()
    publisher_factory.assert_not_called()


def test_preflight_does_not_touch_wechat_when_the_desktop_is_locked() -> None:
    locked = snapshot().model_copy(update={"desktop_unlocked": False})
    publisher_factory = Mock()
    executor = DesktopPublishExecutor(publisher_factory=publisher_factory)

    with patch("src.agent.executor.probe_environment", return_value=locked):
        result = executor.preflight()

    assert result.desktop_unlocked is False
    publisher_factory.assert_not_called()

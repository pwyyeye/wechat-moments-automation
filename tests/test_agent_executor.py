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


def test_preflight_returns_a_safe_snapshot_when_window_preparation_fails() -> None:
    executor = DesktopPublishExecutor(publisher_factory=Mock())

    with (
        patch("src.agent.executor.probe_environment", return_value=snapshot()),
        patch(
            "src.agent.executor.prepare_moments_window",
            side_effect=RuntimeError("window activation denied"),
        ),
    ):
        result = executor.preflight()

    assert result.running is True
    assert result.moments_window_ready is False


def test_publisher_is_initialized_once_before_use() -> None:
    publisher = Mock()
    publisher.initialize.return_value = True
    publisher_factory = Mock(return_value=publisher)
    executor = DesktopPublishExecutor(publisher_factory=publisher_factory)

    assert executor._get_publisher() is publisher
    assert executor._get_publisher() is publisher

    publisher_factory.assert_called_once_with()
    publisher.initialize.assert_called_once_with()

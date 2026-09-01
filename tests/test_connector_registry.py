from src.agent.connectors.registry import windows_desktop_ready
from src.agent.models import AgentSnapshot


def test_windows_executor_is_ready_with_moments_window_closed() -> None:
    snapshot = AgentSnapshot(
        running=True,
        loggedIn=True,
        momentsWindowReady=False,
        wechatVersion="4.1.13.12",
        interactiveSession=True,
        desktopUnlocked=True,
    )

    assert windows_desktop_ready(snapshot)


def test_windows_executor_requires_unlocked_logged_in_wechat() -> None:
    snapshot = AgentSnapshot(
        running=True,
        loggedIn=False,
        momentsWindowReady=False,
        wechatVersion="4.1.13.12",
        interactiveSession=True,
        desktopUnlocked=True,
    )

    assert not windows_desktop_ready(snapshot)

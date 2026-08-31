from dataclasses import dataclass

from src.agent.wechat_identity import _try_activate_window, parse_profile_identity


@dataclass
class Block:
    text: str
    x: int
    y: int
    confidence: float = 0.99


def test_parse_profile_identity_extracts_nickname_and_wechat_id() -> None:
    identity = parse_profile_identity(
        [
            Block("番石榴", 180, 55),
            Block("微信号：higuava001", 220, 95),
            Block("发消息", 200, 140),
        ]
    )

    assert identity is not None
    assert identity.nickname == "番石榴"
    assert identity.wechat_id == "higuava001"


def test_parse_profile_identity_rejects_labels_without_nickname() -> None:
    assert parse_profile_identity([Block("微信号：wxid_demo", 180, 90)]) is None


def test_parse_profile_identity_accepts_split_wechat_id_and_nearest_nickname() -> None:
    identity = parse_profile_identity(
        [
            Block("聊天", 20, 20),
            Block("正确昵称", 205, 68),
            Block("微信号：", 180, 102),
            Block("wxid_split", 255, 103),
        ]
    )

    assert identity is not None
    assert identity.nickname == "正确昵称"
    assert identity.wechat_id == "wxid_split"


def test_activation_failure_is_non_fatal(monkeypatch) -> None:
    import win32api
    import win32gui

    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 100)
    monkeypatch.setattr(
        win32gui,
        "SetForegroundWindow",
        lambda hwnd: (_ for _ in ()).throw(RuntimeError("focus denied")),
    )
    events = []
    monkeypatch.setattr(win32api, "keybd_event", lambda *args: events.append(args))

    assert _try_activate_window(200) is False
    assert len(events) == 2

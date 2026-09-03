from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

import src.agent.wechat_identity as wechat_identity
from src.agent.wechat_identity import (
    WeChatIdentity,
    _profile_avatar_points,
    _try_activate_window,
    parse_profile_identity,
)


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


def test_cached_identity_read_never_starts_ui_detection(monkeypatch) -> None:
    detected = []
    monkeypatch.setattr(wechat_identity, "_cached_identity", None)
    monkeypatch.setattr(wechat_identity, "_cached_hwnd", None)
    monkeypatch.setattr(wechat_identity, "find_wechat_main_window", lambda: 123)
    monkeypatch.setattr(
        wechat_identity,
        "_detect_profile_identity",
        lambda hwnd: detected.append(hwnd),
    )

    assert wechat_identity.get_wechat_identity() is None
    assert detected == []


def test_manual_identity_detection_runs_once_and_populates_cache(monkeypatch) -> None:
    detected = []
    identity = WeChatIdentity("番石榴", "higuava001")
    monkeypatch.setattr(wechat_identity, "_cached_identity", None)
    monkeypatch.setattr(wechat_identity, "_cached_hwnd", None)
    monkeypatch.setattr(wechat_identity, "_cached_at", 0.0)
    monkeypatch.setattr(wechat_identity, "find_wechat_main_window", lambda: 123)
    monkeypatch.setattr(
        wechat_identity,
        "_detect_profile_identity",
        lambda hwnd: detected.append(hwnd) or identity,
    )

    assert wechat_identity.get_wechat_identity(force=True) == identity
    assert wechat_identity.get_wechat_identity() == identity
    assert detected == [123]


def test_cached_identity_is_invalidated_when_wechat_session_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        wechat_identity,
        "_cached_identity",
        WeChatIdentity("旧账号", "old-account"),
    )
    monkeypatch.setattr(wechat_identity, "_cached_hwnd", 123)
    monkeypatch.setattr(wechat_identity, "find_wechat_main_window", lambda: 456)

    assert wechat_identity.get_cached_wechat_identity() is None
    status = wechat_identity.get_wechat_identity_status()
    assert status["code"] == "WECHAT_SESSION_CHANGED"


def test_profile_avatar_points_cover_scaled_and_legacy_layouts() -> None:
    assert _profile_avatar_points(
        left=90,
        top=100,
        client_left=100,
        client_top=130,
        scale=1.5,
    ) == [
        (157, 220),
        (155, 224),
        (145, 194),
    ]


def test_profile_detection_uses_one_click_and_closes_recognized_popover(monkeypatch) -> None:
    import win32api
    import win32gui
    from PIL import ImageGrab

    mouse_events = []
    key_events = []
    engine = SimpleNamespace(
        recognize=lambda image: [
            Block("番石榴", 180, 55),
            Block("微信号：higuava001", 220, 95),
        ]
    )
    user32 = SimpleNamespace(
        SetProcessDpiAwarenessContext=lambda context: None,
        GetDpiForWindow=lambda hwnd: 96,
    )
    monkeypatch.setattr(wechat_identity.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setattr(wechat_identity, "_get_identity_ocr_engine", lambda: engine)
    monkeypatch.setattr(wechat_identity, "_try_activate_window", lambda hwnd: True)
    monkeypatch.setattr(wechat_identity.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 999)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (100, 100, 900, 700))
    monkeypatch.setattr(win32gui, "ClientToScreen", lambda hwnd, point: (100, 130))
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "SetForegroundWindow", lambda hwnd: None)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: None)
    monkeypatch.setattr(win32api, "mouse_event", lambda *args: mouse_events.append(args))
    monkeypatch.setattr(win32api, "keybd_event", lambda *args: key_events.append(args))
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda **kwargs: np.zeros((10, 10, 3), dtype=np.uint8),
    )

    identity = wechat_identity._detect_profile_identity(123)

    assert identity == WeChatIdentity("番石榴", "higuava001")
    assert len(mouse_events) == 2
    assert len(key_events) == 2


def test_profile_detection_tries_legacy_point_after_primary_miss(monkeypatch) -> None:
    import win32api
    import win32gui
    from PIL import ImageGrab

    cursor_points = []
    results = iter(
        [
            [],
            [
                Block("番石榴", 180, 55),
                Block("微信号：higuava001", 220, 95),
            ],
        ]
    )
    engine = SimpleNamespace(recognize=lambda image: next(results))
    user32 = SimpleNamespace(
        SetProcessDpiAwarenessContext=lambda context: None,
        GetDpiForWindow=lambda hwnd: 96,
    )
    monkeypatch.setattr(wechat_identity.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setattr(wechat_identity, "_get_identity_ocr_engine", lambda: engine)
    monkeypatch.setattr(wechat_identity, "_try_activate_window", lambda hwnd: True)
    monkeypatch.setattr(wechat_identity.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 999)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (100, 100, 900, 700))
    monkeypatch.setattr(win32gui, "ClientToScreen", lambda hwnd, point: (100, 130))
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "SetForegroundWindow", lambda hwnd: None)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: cursor_points.append(point))
    monkeypatch.setattr(win32api, "mouse_event", lambda *args: None)
    monkeypatch.setattr(win32api, "keybd_event", lambda *args: None)
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda **kwargs: np.zeros((10, 10, 3), dtype=np.uint8),
    )

    identity = wechat_identity._detect_profile_identity(123)

    assert identity == WeChatIdentity("番石榴", "higuava001")
    assert cursor_points[:2] == [(138, 190), (155, 224)]


def test_profile_detection_does_not_send_blind_escape(monkeypatch) -> None:
    import win32api
    import win32gui
    from PIL import ImageGrab

    key_events = []
    engine = SimpleNamespace(recognize=lambda image: [])
    user32 = SimpleNamespace(
        SetProcessDpiAwarenessContext=lambda context: None,
        GetDpiForWindow=lambda hwnd: 96,
    )
    monkeypatch.setattr(wechat_identity.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setattr(wechat_identity, "_get_identity_ocr_engine", lambda: engine)
    monkeypatch.setattr(wechat_identity, "_try_activate_window", lambda hwnd: True)
    monkeypatch.setattr(wechat_identity.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 999)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (100, 100, 900, 700))
    monkeypatch.setattr(win32gui, "ClientToScreen", lambda hwnd, point: (100, 130))
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "SetForegroundWindow", lambda hwnd: None)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: None)
    monkeypatch.setattr(win32api, "mouse_event", lambda *args: None)
    monkeypatch.setattr(win32api, "keybd_event", lambda *args: key_events.append(args))
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda **kwargs: np.zeros((10, 10, 3), dtype=np.uint8),
    )

    assert wechat_identity._detect_profile_identity(123) is None
    assert key_events == []

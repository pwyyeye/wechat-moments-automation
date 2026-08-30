from unittest.mock import Mock

from src.core.account_manager import WeChatWindowFinder


def test_enum_all_excludes_auxiliary_wechat_windows(monkeypatch) -> None:
    windows = {
        100: {"visible": True, "class": "Qt51514QWindowIcon", "title": "微信"},
        101: {"visible": True, "class": "Qt51514QWindowIcon", "title": "朋友圈"},
        102: {"visible": True, "class": "WeChatMainWndForPC", "title": "工作微信"},
        103: {"visible": True, "class": "Qt51514QWindowIcon", "title": "图片查看"},
        104: {"visible": False, "class": "Qt51514QWindowIcon", "title": "微信"},
    }

    def enum_windows(callback, results) -> None:
        for hwnd in windows:
            callback(hwnd, results)

    monkeypatch.setattr("src.core.account_manager.win32gui.EnumWindows", enum_windows)
    monkeypatch.setattr(
        "src.core.account_manager.win32gui.IsWindowVisible",
        lambda hwnd: windows[hwnd]["visible"],
    )
    monkeypatch.setattr(
        "src.core.account_manager.win32gui.GetClassName",
        lambda hwnd: windows[hwnd]["class"],
    )
    monkeypatch.setattr(
        "src.core.account_manager.win32gui.GetWindowText",
        lambda hwnd: windows[hwnd]["title"],
    )

    assert WeChatWindowFinder.enum_all() == [(100, "微信"), (102, "工作微信")]


def test_find_by_name_uses_main_windows_only(monkeypatch) -> None:
    enum_all = Mock(return_value=[(100, "微信")])
    monkeypatch.setattr(WeChatWindowFinder, "enum_all", enum_all)

    assert WeChatWindowFinder.find_by_name("微信") == 100
    enum_all.assert_called_once_with()

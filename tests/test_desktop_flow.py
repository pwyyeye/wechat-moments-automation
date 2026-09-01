from unittest.mock import Mock

from src.core.publisher import EventDrivenPublisher
from src.executor.operator import Operator
from src.locator.ocr_locator import TextBlock


def build_desktop_publisher(ocr_results) -> EventDrivenPublisher:
    publisher = EventDrivenPublisher.__new__(EventDrivenPublisher)
    publisher.operator = Mock()
    publisher.operator.active_window_region.return_value = (1000, 400, 600, 800)
    publisher.operator.click_moments_camera.return_value = True
    publisher.operator.activate_moments_window.return_value = True
    publisher.file_dialog = Mock()
    publisher.file_dialog.select_file_via_pywinauto.return_value = True
    publisher.ocr = Mock()
    publisher.ocr.scan_screen.side_effect = ocr_results
    publisher._prepared_image_count = 0
    return publisher


def text_block(text: str) -> TextBlock:
    return TextBlock(text, 1, 1, 10, 10, 0.99, [])


def test_desktop_editor_selects_required_first_image(monkeypatch) -> None:
    publisher = build_desktop_publisher([[], [text_block("这一刻的想法")]])
    monkeypatch.setattr("src.core.publisher.time.sleep", Mock())

    assert publisher._prepare_desktop_editor(["first.png"])
    assert publisher._prepared_image_count == 1
    publisher.operator.click_moments_camera.assert_called_once_with()
    publisher.file_dialog.select_file_via_pywinauto.assert_called_once_with(
        "first.png",
        timeout=12.0,
    )


def test_desktop_editor_reuses_an_open_compose_panel() -> None:
    publisher = build_desktop_publisher([[text_block("这一刻的想法")]])

    assert publisher._prepare_desktop_editor(["first.png"])
    assert publisher._prepared_image_count == 1
    publisher.operator.click_moments_camera.assert_not_called()
    publisher.file_dialog.select_file_via_pywinauto.assert_not_called()


def test_desktop_editor_reuses_a_populated_compose_panel() -> None:
    publisher = build_desktop_publisher(
        [[text_block("发表"), text_block("谁可以看")]]
    )

    assert publisher._prepare_desktop_editor(["first.png"])
    assert publisher._prepared_image_count == 1
    publisher.operator.click_moments_camera.assert_not_called()
    publisher.file_dialog.select_file_via_pywinauto.assert_not_called()


def test_desktop_editor_requires_an_image_to_open() -> None:
    publisher = build_desktop_publisher([[]])

    assert not publisher._prepare_desktop_editor([])
    publisher.operator.click_moments_camera.assert_not_called()


def test_camera_click_uses_safe_header_position() -> None:
    operator = Operator.__new__(Operator)
    operator.sim = Mock()
    operator.activate_moments_window = Mock(return_value=True)
    operator.active_window_region = Mock(return_value=(1000, 400, 600, 800))

    assert operator.click_moments_camera()
    operator.sim.click_at.assert_called_once_with(1098, 430)


def test_editor_body_click_uses_safe_compose_position() -> None:
    operator = Operator.__new__(Operator)
    operator.sim = Mock()
    operator.activate_moments_window = Mock(return_value=True)
    operator.active_window_region = Mock(return_value=(1000, 400, 600, 800))

    assert operator.click_moments_editor_body()
    operator.sim.click_at.assert_called_once_with(1210, 576)


def test_open_moments_navigation_uses_semantic_uia_command() -> None:
    operator = Operator.__new__(Operator)
    operator._uia = Mock()
    operator._uia.available = True
    operator._uia.open_moments.return_value = True

    assert operator.open_moments_navigation(timeout=4.0)
    operator._uia.open_moments.assert_called_once_with(timeout=4.0)


def test_enter_moments_falls_back_when_uia_navigation_is_unavailable() -> None:
    operator = Operator.__new__(Operator)
    operator.open_moments_navigation = Mock(return_value=False)
    operator.click_element = Mock(return_value=True)
    nav_element = Mock()
    verify_element = Mock()

    assert operator.enter_moments(nav_element, verify_element)
    operator.click_element.assert_called_once_with(
        nav_element,
        verify_element=verify_element,
    )


def test_populated_editor_text_is_replaced_via_safe_body_focus(
    monkeypatch,
) -> None:
    publisher = EventDrivenPublisher.__new__(EventDrivenPublisher)
    publisher.operator = Mock()
    publisher.operator.click_element.return_value = False
    publisher.operator.active_window_region.return_value = (1000, 400, 600, 800)
    publisher.operator.click_moments_editor_body.return_value = True
    publisher.ocr = Mock()
    publisher.ocr.scan_screen.return_value = [
        text_block("发表"),
        text_block("谁可以看"),
    ]
    publisher.sim = Mock()
    publisher._watch_manager = Mock()
    publisher.bus = Mock()
    hotkey = Mock()
    monkeypatch.setattr("pyautogui.hotkey", hotkey)

    assert publisher._step_type_text("冻结文案")
    publisher.operator.click_moments_editor_body.assert_called_once_with()
    hotkey.assert_called_once_with("ctrl", "a")
    publisher.sim.type_text.assert_called_once_with("冻结文案")
    publisher._watch_manager.after.assert_called_once_with(
        0.5,
        {"reason": "typing_complete"},
    )


def test_moments_window_can_belong_to_a_separate_weixin_process(
    monkeypatch,
) -> None:
    operator = Operator.__new__(Operator)
    operator._wechat_hwnd = 111
    operator._moments_hwnd = None
    monkeypatch.setattr(
        "src.executor.operator.win32gui.EnumWindows",
        lambda callback, value: callback(222, value),
    )
    monkeypatch.setattr(
        "src.executor.operator.win32gui.IsWindowVisible",
        lambda hwnd: True,
    )
    monkeypatch.setattr(
        "src.executor.operator.win32gui.IsWindow",
        lambda hwnd: True,
    )
    monkeypatch.setattr(
        "src.executor.operator._get_window_text",
        lambda hwnd: "朋友圈",
    )
    monkeypatch.setattr(
        "src.executor.operator.win32process.GetWindowThreadProcessId",
        lambda hwnd: (1, 9002 if hwnd == 222 else 9001),
    )
    monkeypatch.setattr(
        "src.executor.wechat_discovery._get_process_name",
        lambda pid: "Weixin.exe",
    )

    assert operator.find_moments_window()
    assert operator._moments_hwnd == 222


def test_confirmed_publish_closes_only_the_separate_moments_window(
    monkeypatch,
) -> None:
    operator = Operator.__new__(Operator)
    operator._wechat_hwnd = 111
    operator._moments_hwnd = 222
    operator._active_hwnd = 222
    window_exists = Mock(side_effect=[True, True, False, False])
    post_message = Mock()
    monkeypatch.setattr(
        "src.executor.operator.win32gui.IsWindow",
        window_exists,
    )
    monkeypatch.setattr(
        "src.executor.operator._get_window_text",
        lambda hwnd: "朋友圈" if hwnd == 222 else "微信",
    )
    monkeypatch.setattr(
        "src.executor.operator.win32process.GetWindowThreadProcessId",
        lambda hwnd: (1, 9002 if hwnd == 222 else 9001),
    )
    monkeypatch.setattr(
        "src.executor.wechat_discovery._get_process_name",
        lambda pid: "Weixin.exe",
    )
    monkeypatch.setattr(
        "src.executor.operator.win32gui.PostMessage",
        post_message,
    )
    monkeypatch.setattr("src.executor.operator.time.sleep", Mock())

    assert operator.close_moments_window()
    post_message.assert_called_once_with(222, 16, 0, 0)
    assert operator._moments_hwnd is None
    assert operator._active_hwnd == 111


def test_moments_cleanup_refuses_to_close_the_main_wechat_window(
    monkeypatch,
) -> None:
    operator = Operator.__new__(Operator)
    operator._wechat_hwnd = 111
    operator._moments_hwnd = 111
    operator._active_hwnd = 111
    monkeypatch.setattr(
        "src.executor.operator.win32gui.IsWindow",
        lambda hwnd: True,
    )
    monkeypatch.setattr(
        "src.executor.operator._get_window_text",
        lambda hwnd: "朋友圈",
    )
    monkeypatch.setattr(
        "src.executor.operator.win32process.GetWindowThreadProcessId",
        lambda hwnd: (1, 9001),
    )
    monkeypatch.setattr(
        "src.executor.wechat_discovery._get_process_name",
        lambda pid: "Weixin.exe",
    )
    post_message = Mock()
    monkeypatch.setattr(
        "src.executor.operator.win32gui.PostMessage",
        post_message,
    )

    assert not operator.close_moments_window()
    post_message.assert_not_called()


def test_login_fallback_scans_only_the_main_window() -> None:
    operator = Operator.__new__(Operator)
    operator._uia = None
    operator.router = Mock()
    operator.router.ocr.scan_screen.return_value = [
        TextBlock("Q 搜索", 10, 10, 20, 10, 0.99, [])
    ]
    operator.activate_main_window = Mock(return_value=True)
    operator.active_window_region = Mock(return_value=(1000, 400, 600, 800))

    result = operator.check_login_state()

    assert result["logged_in"]
    operator.router.ocr.scan_screen.assert_called_once_with(
        region=(1000, 400, 600, 400)
    )


def test_login_accepts_visible_desktop_moments_when_uia_tree_is_unavailable(
    monkeypatch,
) -> None:
    operator = Operator.__new__(Operator)
    operator._uia = Mock()
    operator._uia.available = True
    operator._uia.check_login.return_value = {
        "isLoggedIn": False,
        "detectedPage": "检测失败",
        "navLabels": [],
    }
    operator._moments_hwnd = 123
    operator.find_moments_window = Mock(return_value=True)
    operator.activate_moments_window = Mock(return_value=True)
    monkeypatch.setattr(
        "src.executor.operator.win32gui.GetWindowRect",
        Mock(return_value=(1000, 400, 1600, 1200)),
    )

    result = operator.check_login_state()

    assert result == {
        "logged_in": True,
        "page": "朋友圈",
        "details": "独立朋友圈窗口已就绪",
    }

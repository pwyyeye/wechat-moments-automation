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
    publisher.ocr.find_best.side_effect = ocr_results
    publisher._prepared_image_count = 0
    return publisher


def test_desktop_editor_selects_required_first_image(monkeypatch) -> None:
    publisher = build_desktop_publisher([None, object()])
    monkeypatch.setattr("src.core.publisher.time.sleep", Mock())

    assert publisher._prepare_desktop_editor(["first.png"])
    assert publisher._prepared_image_count == 1
    publisher.operator.click_moments_camera.assert_called_once_with()
    publisher.file_dialog.select_file_via_pywinauto.assert_called_once_with(
        "first.png",
        timeout=12.0,
    )


def test_desktop_editor_reuses_an_open_compose_panel() -> None:
    publisher = build_desktop_publisher([object()])

    assert publisher._prepare_desktop_editor(["first.png"])
    publisher.operator.click_moments_camera.assert_not_called()
    publisher.file_dialog.select_file_via_pywinauto.assert_not_called()


def test_desktop_editor_requires_an_image_to_open() -> None:
    publisher = build_desktop_publisher([None])

    assert not publisher._prepare_desktop_editor([])
    publisher.operator.click_moments_camera.assert_not_called()


def test_camera_click_uses_safe_header_position() -> None:
    operator = Operator.__new__(Operator)
    operator.sim = Mock()
    operator.activate_moments_window = Mock(return_value=True)
    operator.active_window_region = Mock(return_value=(1000, 400, 600, 800))

    assert operator.click_moments_camera()
    operator.sim.click_at.assert_called_once_with(1098, 430)


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
        "src.executor.operator.win32gui.GetWindowText",
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

from unittest.mock import Mock

from src.locator.ocr_locator import OCRLocator, TextBlock
from src.locator.router import ElementDescriptor, LocateRouter


def make_block() -> TextBlock:
    return TextBlock(
        text="取消",
        x=20,
        y=30,
        width=40,
        height=20,
        confidence=0.99,
        box=[[0, 20], [40, 20], [40, 40], [0, 40]],
    )


def test_region_scan_returns_absolute_screen_coordinates(monkeypatch) -> None:
    locator = OCRLocator.__new__(OCRLocator)
    locator._cache = None
    locator._cache_ttl = 2.0
    locator._engine = Mock()
    locator._engine.recognize.return_value = [make_block()]

    monkeypatch.setattr("src.locator.ocr_locator.pyautogui.screenshot", Mock())
    monkeypatch.setattr(
        "src.locator.ocr_locator.pyautogui.size",
        Mock(return_value=(2560, 1440)),
    )
    monkeypatch.setattr("src.locator.ocr_locator.np.array", Mock(return_value=Mock()))

    block = locator.scan_screen(region=(1000, 400, 600, 800))[0]

    assert (block.x, block.y) == (1020, 430)
    assert block.box == [[1000, 420], [1040, 420], [1040, 440], [1000, 440]]


def test_region_cache_is_not_reused_for_full_screen(monkeypatch) -> None:
    locator = OCRLocator.__new__(OCRLocator)
    locator._cache = None
    locator._cache_ttl = 2.0
    locator._engine = Mock()
    locator._engine.recognize.side_effect = [[make_block()], [make_block()]]

    monkeypatch.setattr("src.locator.ocr_locator.pyautogui.screenshot", Mock())
    monkeypatch.setattr(
        "src.locator.ocr_locator.pyautogui.size",
        Mock(return_value=(2560, 1440)),
    )
    monkeypatch.setattr("src.locator.ocr_locator.np.array", Mock(return_value=Mock()))

    locator.scan_screen(region=(1000, 400, 600, 800))
    locator.scan_screen()

    assert locator._engine.recognize.call_count == 2


def test_router_passes_element_ocr_region() -> None:
    ocr = Mock()
    ocr.find_best.return_value = make_block()
    router = LocateRouter(
        ocr=ocr,
        feature=Mock(),
        calibrator=Mock(),
        config={"failure_screenshots_dir": ".validation/test-failures"},
    )
    region = (1000, 400, 600, 800)

    result = router.locate(
        ElementDescriptor(name="取消", ocr_text="取消", ocr_region=region)
    )

    assert result is not None
    ocr.find_best.assert_called_once_with("取消", region=region)

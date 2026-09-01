import sys
import types
from pathlib import Path

import numpy as np
import pytest

from src.locator.ocr_locator import PaddleOCREngine, resolve_ocr_model_root


class FakeV3Result:
    json = {
        "res": {
            "rec_texts": ["朋友圈"],
            "rec_scores": [0.996],
            "rec_polys": [[[25, 30], [199, 30], [199, 104], [25, 104]]],
        }
    }


class FakeV3OCR:
    def predict(self, image):
        assert image.shape == (10, 10, 3)
        return [FakeV3Result()]


class FakeV2OCR:
    def ocr(self, image, cls=False):
        assert image.shape == (10, 10, 3)
        assert cls is False
        return [[
            [
                [[25, 30], [199, 30], [199, 104], [25, 104]],
                ("朋友圈", 0.996),
            ]
        ]]


def test_recognize_supports_paddleocr_v3_results() -> None:
    engine = PaddleOCREngine()
    engine._ocr = FakeV3OCR()
    engine._api_version = 3

    blocks = engine.recognize(np.zeros((10, 10, 3), dtype=np.uint8))

    assert len(blocks) == 1
    assert blocks[0].text == "朋友圈"
    assert blocks[0].x == 112
    assert blocks[0].y == 67
    assert blocks[0].confidence == 0.996


def test_recognize_keeps_paddleocr_v2_compatibility() -> None:
    engine = PaddleOCREngine()
    engine._ocr = FakeV2OCR()
    engine._api_version = 2

    blocks = engine.recognize(np.zeros((10, 10, 3), dtype=np.uint8))

    assert len(blocks) == 1
    assert blocks[0].text == "朋友圈"
    assert blocks[0].width == 174
    assert blocks[0].height == 74


def _create_fake_models(root: Path) -> None:
    for model_name in ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"):
        model_dir = root / model_name
        model_dir.mkdir(parents=True)
        for filename in ("inference.json", "inference.pdiparams", "inference.yml"):
            (model_dir / filename).write_bytes(b"test")


def test_resolve_ocr_model_root_from_environment(tmp_path, monkeypatch) -> None:
    _create_fake_models(tmp_path)
    monkeypatch.setenv("WECHAT_PUBLISHER_OCR_MODEL_ROOT", str(tmp_path))

    assert resolve_ocr_model_root() == tmp_path.resolve()


def test_frozen_runtime_requires_complete_bundled_models(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WECHAT_PUBLISHER_OCR_MODEL_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    with pytest.raises(RuntimeError, match="Bundled OCR models"):
        resolve_ocr_model_root()


def test_frozen_runtime_uses_bundled_models(tmp_path, monkeypatch) -> None:
    model_root = tmp_path / "models" / "paddleocr"
    _create_fake_models(model_root)
    monkeypatch.delenv("WECHAT_PUBLISHER_OCR_MODEL_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert resolve_ocr_model_root() == model_root


def test_paddleocr_initialization_failure_is_latched(monkeypatch) -> None:
    attempts = []

    def fail_initialization(**kwargs):
        attempts.append(kwargs)
        raise RuntimeError("The pipeline (OCR) does not exist!")

    monkeypatch.setitem(
        sys.modules,
        "paddleocr",
        types.SimpleNamespace(PaddleOCR=fail_initialization),
    )
    monkeypatch.setattr(
        "src.locator.ocr_locator.resolve_ocr_model_root",
        lambda config=None: None,
    )
    engine = PaddleOCREngine()
    image = np.zeros((10, 10, 3), dtype=np.uint8)

    assert engine.recognize(image) == []
    assert engine.recognize(image) == []
    assert len(attempts) == 1

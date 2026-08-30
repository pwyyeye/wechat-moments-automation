import numpy as np

from src.locator.ocr_locator import PaddleOCREngine


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

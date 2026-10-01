"""등록증 OCR 저해상도 폴백 — tesseract 우선, 저신뢰·소형 이미지만 PaddleOCR 로 재시도.

2026-10-01 진아244 실측: tesseract 10/12, PaddleOCR 원본 7/12, 줄 병합 후 10/12.
여기서는 tesseract/PaddleOCR 를 부르지 않고 모킹으로 경로 선택만 검사한다.
"""
from __future__ import annotations

import asyncio
import base64
import sys

import pytest

from app.core import local_ocr_bridge as bridge

GOOD_TEXT = "\n".join([
    "사 업 자 등 록 증",
    "등록번호 : 123-45-67891",
    "상 호 : 언니냉면 테스트",
    "성 명 : 김테스트",
    "개 업 연 월 일 : 2020 년 03 월 02 일",
    "사업장 소재지 : 서울특별시 성북구 동소문로 1",
])
WEAK_TEXT = "ASSIA 123-45-67891"
IMAGE = base64.b64encode(b"image-bytes").decode("ascii")


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "local")
    monkeypatch.delenv("OCR_FALLBACK_ENGINE", raising=False)
    monkeypatch.delenv("OCR_FALLBACK_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("OCR_FALLBACK_MIN_LONG_SIDE", raising=False)
    monkeypatch.setitem(bridge._paddle_state, "available", True)
    monkeypatch.setattr(bridge, "_long_side", lambda data: 2000)


def _tess(text: str, confidence: float):
    return lambda data, language=bridge.DEFAULT_LANGUAGE: {
        "text": text, "confidence": confidence, "language": language,
        "error": None, "backend": "local", "engine": "tesseract"}


def _paddle(text: str, calls: list):
    def fake(data, language="korean"):
        calls.append(data)
        return {"text": text, "confidence": 0.9, "language": language,
                "error": None, "backend": "local", "engine": "paddle"}
    return fake


def _run():
    return asyncio.run(bridge.ocr_extract(image_base64=IMAGE))


def _box(text, x, y, h=20, score=0.9):
    return {"text": text, "score": score, "x": x, "y": y, "h": h}


def test_merge_boxes_joins_same_y_and_sorts_by_x():
    text = bridge.merge_boxes_to_lines([
        _box("김테스트", 300, 102),
        _box("성 명 :", 100, 100),
        _box("상 호", 100, 200),
    ])
    assert text.splitlines() == ["성 명 : 김테스트", "상 호"]


def test_merge_boxes_keeps_different_heights_on_separate_lines():
    text = bridge.merge_boxes_to_lines([
        _box("작은글자", 100, 100, h=20),
        _box("큰제목", 300, 100, h=20),
        _box("다른줄", 500, 140, h=20),
    ])
    assert text.splitlines() == ["작은글자 큰제목", "다른줄"]


def test_merge_boxes_empty():
    assert bridge.merge_boxes_to_lines([]) == ""
    assert bridge.merge_boxes_to_lines([_box("  ", 0, 0)]) == ""


def test_fallback_off_by_default_never_calls_paddle(monkeypatch):
    calls: list = []
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(WEAK_TEXT, 0.3))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(GOOD_TEXT, calls))
    result = _run()
    assert calls == []
    assert result["text"] == WEAK_TEXT
    assert result["engine"] == "tesseract"


def test_fallback_on_low_confidence_picks_paddle_with_more_fields(monkeypatch):
    calls: list = []
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(WEAK_TEXT, 0.3))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(GOOD_TEXT, calls))
    result = _run()
    assert len(calls) == 1
    assert result["engine"] == "paddle"
    assert result["text"] == GOOD_TEXT


def test_fallback_on_small_image_triggers_even_with_high_confidence(monkeypatch):
    calls: list = []
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "_long_side", lambda data: 835)
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(GOOD_TEXT.replace("서울특별시", "ASSIA"), 0.95))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(WEAK_TEXT, calls))
    result = _run()
    assert len(calls) == 1
    assert result["engine"] == "tesseract"  # paddle 이 더 적게 채웠으므로 tesseract 유지


def test_fallback_skipped_when_confident_large_and_complete(monkeypatch):
    calls: list = []
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(GOOD_TEXT, 0.95))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(GOOD_TEXT, calls))
    assert _run()["engine"] == "tesseract"
    assert calls == []


def test_fallback_triggers_when_fewer_than_three_fields(monkeypatch):
    calls: list = []
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(WEAK_TEXT, 0.95))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(GOOD_TEXT, calls))
    assert _run()["engine"] == "paddle"
    assert len(calls) == 1


def test_fallback_tie_keeps_tesseract(monkeypatch):
    calls: list = []
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(GOOD_TEXT, 0.5))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(GOOD_TEXT, calls))
    assert _run()["engine"] == "tesseract"
    assert len(calls) == 1


def test_paddle_missing_returns_tesseract_without_error(monkeypatch):
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(WEAK_TEXT, 0.3))

    def missing(*args, **kwargs):
        raise ImportError("No module named 'paddleocr'")

    monkeypatch.setattr(bridge, "paddle_extract", missing)
    result = _run()
    assert result["text"] == WEAK_TEXT
    assert result["engine"] == "tesseract"


def test_paddle_init_failure_disables_fallback(monkeypatch):
    monkeypatch.setattr(bridge, "_paddle_engines", {})
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # import 시 ImportError
    with pytest.raises(ImportError):
        bridge._paddle_engine("korean")
    assert bridge.paddle_available() is False

    calls: list = []
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "paddle")
    monkeypatch.setattr(bridge, "tesseract_extract", _tess(WEAK_TEXT, 0.3))
    monkeypatch.setattr(bridge, "paddle_extract", _paddle(GOOD_TEXT, calls))
    assert _run()["engine"] == "tesseract"
    assert calls == []


def test_result_always_has_engine_key(monkeypatch):
    monkeypatch.setattr(bridge, "tesseract_extract", lambda data, language=bridge.DEFAULT_LANGUAGE: {
        "text": "x", "confidence": 0.9, "language": language, "error": None, "backend": "local"})
    assert _run()["engine"] == "tesseract"


def test_fallback_engine_values(monkeypatch):
    assert bridge.fallback_engine() == "off"
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "bogus")
    assert bridge.fallback_engine() == "off"
    monkeypatch.setenv("OCR_FALLBACK_ENGINE", "Paddle")
    assert bridge.fallback_engine() == "paddle"


def test_paddle_extract_uses_safe_setup_and_downscales(monkeypatch):
    np = pytest.importorskip("numpy")
    pil = pytest.importorskip("PIL.Image")
    import io
    import types

    seen: dict = {}

    class FakePaddle:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def predict(self, image):
            seen["shape"] = np.asarray(image).shape
            poly = lambda x, y: [[x, y - 10], [x + 50, y - 10], [x + 50, y + 10], [x, y + 10]]  # noqa: E731
            return [{"res": {
                "rec_texts": ["성 명 :", "김테스트"],
                "rec_scores": [0.9, 0.8],
                "rec_polys": [poly(10, 100), poly(100, 101)],
            }}]

    monkeypatch.setattr(bridge, "_paddle_engines", {})
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddle))
    buffer = io.BytesIO()
    pil.new("RGB", (3200, 1600), "white").save(buffer, format="PNG")

    result = bridge.paddle_extract(buffer.getvalue())

    assert seen["init"]["enable_mkldnn"] is False
    assert seen["init"]["lang"] == "korean"
    assert max(seen["shape"][:2]) <= bridge.PADDLE_MAX_LONG_SIDE
    assert result["text"] == "성 명 : 김테스트"
    assert result["engine"] == "paddle"
    assert result["confidence"] == 0.85

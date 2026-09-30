"""OCR 경로 선택 — PC Agent 없는 호스트(진아서버)에서 서버 tesseract 로 넘어가는지.

2026-10-01: 진아서버는 127.0.0.1:8102 가 없어 사업자등록증 OCR 이 전부 실패하고
4항목 손입력만 가능했다. 여기서는 tesseract 바이너리를 부르지 않고
경로 선택·TSV 해석·언어 선택만 검사한다.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from app.core import local_ocr_bridge as bridge

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _row(block: int, par: int, line: int, word: int, conf: float, text: str) -> str:
    return f"5\t1\t{block}\t{par}\t{line}\t{word}\t0\t0\t10\t10\t{conf}\t{text}"


def test_parse_tsv_restores_lines_and_confidence():
    tsv = "\n".join([
        TSV_HEADER,
        _row(1, 1, 1, 1, 90, "사업자등록번호"),
        _row(1, 1, 1, 2, 80, "123-45-67890"),
        _row(1, 1, 2, 1, 70, "대표자"),
        _row(1, 1, 2, 2, 60, "홍길동"),
    ])
    text, confidence = bridge.parse_tsv(tsv)
    assert text == "사업자등록번호 123-45-67890\n대표자 홍길동"
    assert confidence == 0.75


def test_parse_tsv_skips_empty_and_negative_confidence():
    tsv = "\n".join([
        TSV_HEADER,
        _row(1, 1, 1, 1, -1, "버려질단어"),
        _row(1, 1, 1, 2, 50, " "),
        _row(1, 1, 1, 3, 50, "상호"),
    ])
    text, confidence = bridge.parse_tsv(tsv)
    assert text == "상호"
    assert confidence == 0.5


@pytest.mark.parametrize("tsv", ["", "쓸모없는\t헤더", "level\tconf"])
def test_parse_tsv_rejects_unusable_input(tsv):
    assert bridge.parse_tsv(tsv) == ("", 0.0)


def test_parse_tsv_separates_blocks_with_same_line_number():
    tsv = "\n".join([
        TSV_HEADER,
        _row(1, 1, 1, 1, 90, "왼쪽"),
        _row(2, 1, 1, 1, 90, "오른쪽"),
    ])
    text, _ = bridge.parse_tsv(tsv)
    assert text == "왼쪽\n오른쪽"


@pytest.mark.parametrize(
    ("requested", "installed", "expected"),
    [
        ("kor+eng", {"kor", "eng", "osd"}, "kor+eng"),
        ("kor+eng", {"eng"}, "eng"),
        ("kor", {"eng", "osd"}, "eng"),
        ("kor", set(), "eng"),
        ("", {"kor"}, "kor"),
        ("jpn", {"kor"}, "kor"),
    ],
)
def test_select_langs_keeps_only_installed(requested, installed, expected):
    assert bridge.select_langs(requested, installed) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "auto"), ("", "auto"), ("local", "local"), ("LOCAL", "local"),
     ("pc_agent", "pc_agent"), ("없는값", "auto")],
)
def test_backend_reads_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("OCR_BACKEND", raising=False)
    else:
        monkeypatch.setenv("OCR_BACKEND", value)
    assert bridge.backend() == expected


def test_tesseract_extract_without_binary_is_explicit(monkeypatch):
    monkeypatch.setattr(bridge, "_tesseract_bin", lambda: None)
    with pytest.raises(RuntimeError, match="tesseract"):
        bridge.tesseract_extract(b"\x89PNG")


def test_local_backend_skips_pc_agent(monkeypatch):
    """OCR_BACKEND=local 이면 PC Agent 를 아예 부르지 않는다."""
    monkeypatch.setenv("OCR_BACKEND", "local")
    called: list[bytes] = []

    async def unreachable(*args, **kwargs):
        raise AssertionError("PC Agent 를 불렀다")

    def fake_tesseract(data, language=bridge.DEFAULT_LANGUAGE):
        called.append(data)
        return {"text": "상호 언니냉면", "confidence": 0.9, "language": language,
                "error": None, "backend": "local"}

    monkeypatch.setattr(bridge, "pc_agent_extract", unreachable)
    monkeypatch.setattr(bridge, "tesseract_extract", fake_tesseract)

    result = asyncio.run(bridge.ocr_extract(
        image_base64=base64.b64encode(b"image-bytes").decode("ascii")))
    assert result["backend"] == "local"
    assert result["text"] == "상호 언니냉면"
    assert called == [b"image-bytes"]


def test_auto_backend_falls_back_when_pc_agent_missing(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "auto")

    async def no_agent(*args, **kwargs):
        raise RuntimeError("PC Agent 연결 없음 — OCR 불가")

    monkeypatch.setattr(bridge, "pc_agent_extract", no_agent)
    monkeypatch.setattr(bridge, "tesseract_extract", lambda data, language=bridge.DEFAULT_LANGUAGE: {
        "text": "대표자 홍길동", "confidence": 0.8, "language": language,
        "error": None, "backend": "local"})

    result = asyncio.run(bridge.ocr_extract(image_base64=base64.b64encode(b"x").decode("ascii")))
    assert result["backend"] == "local"


def test_pc_agent_backend_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "pc_agent")

    async def no_agent(*args, **kwargs):
        raise RuntimeError("PC Agent 연결 없음 — OCR 불가")

    def unreachable(*args, **kwargs):
        raise AssertionError("tesseract 로 넘어갔다")

    monkeypatch.setattr(bridge, "pc_agent_extract", no_agent)
    monkeypatch.setattr(bridge, "tesseract_extract", unreachable)
    with pytest.raises(RuntimeError, match="PC Agent 연결 없음"):
        asyncio.run(bridge.ocr_extract(image_base64=base64.b64encode(b"x").decode("ascii")))


def test_both_backends_failing_reports_both(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "auto")

    async def no_agent(*args, **kwargs):
        raise RuntimeError("PC Agent 연결 없음")

    def no_tesseract(*args, **kwargs):
        raise RuntimeError("tesseract 실행 파일이 없습니다")

    monkeypatch.setattr(bridge, "pc_agent_extract", no_agent)
    monkeypatch.setattr(bridge, "tesseract_extract", no_tesseract)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(bridge.ocr_extract(image_base64=base64.b64encode(b"x").decode("ascii")))
    message = str(excinfo.value)
    assert "pc_agent:" in message and "local:" in message


def test_ocr_extract_requires_an_input(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "local")
    with pytest.raises(ValueError, match="image_url 또는 image_base64"):
        asyncio.run(bridge.ocr_extract())


def test_local_backend_rejects_non_http_url(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "local")
    monkeypatch.setattr(bridge, "tesseract_extract",
                        lambda *a, **k: pytest.fail("bytes 를 못 얻었는데 OCR 을 불렀다"))
    with pytest.raises(ValueError, match="image_base64"):
        asyncio.run(bridge.ocr_extract(image_url="/home/partner/obys/data/a.png"))


def test_tesseract_command_uses_tuned_psm_and_langs(monkeypatch):
    """psm 기본값 4 는 실측으로 고른 값이다 — 되돌아가면 읽히는 항목이 준다."""
    captured: dict[str, list[str]] = {}

    class Langs:
        returncode = 0
        stdout = "List of available languages (3):\nkor\neng\nosd\n"
        stderr = ""

    class Done:
        returncode = 0
        stdout = TSV_HEADER + "\n" + _row(1, 1, 1, 1, 90, "상호")
        stderr = ""

    def fake_run(command, **kwargs):
        if "--list-langs" in command:
            return Langs()
        captured["command"] = list(command)
        return Done()

    monkeypatch.setattr(bridge, "_tesseract_bin", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.delenv("OCR_TESSERACT_PSM", raising=False)

    result = bridge.tesseract_extract(b"\x89PNG fake image")
    command = captured["command"]
    assert command[command.index("--psm") + 1] == "4"
    assert command[command.index("-l") + 1] == "kor+eng"
    assert command[-1] == "tsv"
    assert result["text"] == "상호"
    assert result["confidence"] == 0.9

    monkeypatch.setenv("OCR_TESSERACT_PSM", "6")
    bridge.tesseract_extract(b"\x89PNG fake image")
    command = captured["command"]
    assert command[command.index("--psm") + 1] == "6"


def test_tesseract_failure_surfaces_stderr(monkeypatch):
    class Langs:
        returncode = 0
        stdout = "List:\neng\n"
        stderr = ""

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Error in pixReadStream: Pdf reading is not supported\n"

    def fake_run(command, **kwargs):
        return Langs() if "--list-langs" in command else Failed()

    monkeypatch.setattr(bridge, "_tesseract_bin", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="rc=1"):
        bridge.tesseract_extract(b"not-an-image")


def _captured_tesseract(monkeypatch) -> dict:
    """tesseract 에 실제로 넘어간 파일 바이트를 잡아 둔다."""
    seen: dict[str, bytes] = {}

    class Langs:
        returncode = 0
        stdout = "List:\nkor\neng\n"
        stderr = ""

    class Done:
        returncode = 0
        stdout = TSV_HEADER + "\n" + _row(1, 1, 1, 1, 90, "상호")
        stderr = ""

    def fake_run(command, **kwargs):
        if "--list-langs" in command:
            return Langs()
        seen["payload"] = __import__("pathlib").Path(command[1]).read_bytes()
        return Done()

    monkeypatch.setattr(bridge, "_tesseract_bin", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    return seen


def test_preprocess_converts_to_grayscale_png(monkeypatch):
    """JPEG 원본을 그대로 넘기면 진아 실측에서 한글 라벨을 놓쳤다."""
    Image = pytest.importorskip("PIL.Image")
    import io as _io

    source = _io.BytesIO()
    Image.new("RGB", (40, 20), (200, 30, 30)).save(source, format="JPEG")
    jpeg = source.getvalue()
    assert jpeg[:2] == b"\xff\xd8"

    seen = _captured_tesseract(monkeypatch)
    monkeypatch.delenv("OCR_PREPROCESS", raising=False)
    bridge.tesseract_extract(jpeg)

    payload = seen["payload"]
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(_io.BytesIO(payload)) as converted:
        assert converted.mode == "L"
        assert converted.size == (40, 20)


def test_preprocess_can_be_switched_off(monkeypatch):
    seen = _captured_tesseract(monkeypatch)
    monkeypatch.setenv("OCR_PREPROCESS", "0")
    bridge.tesseract_extract(b"raw-bytes-not-an-image")
    assert seen["payload"] == b"raw-bytes-not-an-image"


def test_preprocess_failure_falls_back_to_original(monkeypatch):
    """이미지로 열리지 않으면 원본을 그대로 넘기고 계속한다."""
    seen = _captured_tesseract(monkeypatch)
    monkeypatch.delenv("OCR_PREPROCESS", raising=False)
    bridge.tesseract_extract(b"not-an-image-at-all")
    assert seen["payload"] == b"not-an-image-at-all"


def test_preprocess_returns_none_without_pillow(monkeypatch):
    monkeypatch.delenv("OCR_PREPROCESS", raising=False)
    real_import = __import__

    def no_pillow(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no Pillow here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_pillow)
    assert bridge._to_grayscale_png(b"\x89PNG\r\n\x1a\n") is None

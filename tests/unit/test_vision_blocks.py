"""AADS-VISION-UNIFY: 비전 입력 통합 — extract_image_blocks 변환 파이프라인.

이미지 픽스처는 Pillow 로 테스트 안에서 만든다 (바이너리를 커밋하지 않는다).
"""
import base64
import io
import logging

import pytest

from app.core import document_context as dc
from app.core.document_context import IMAGE_MAX_BYTES, extract_image_blocks

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


# ── 픽스처 헬퍼 ──────────────────────────────────────────────────────

def _img_bytes(fmt: str, size=(64, 48), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


def _noisy_png_bytes(target_bytes: int) -> bytes:
    """압축이 거의 안 되는 큰 PNG — 리샘플 경로를 타게 하려고 난수 픽셀을 쓴다."""
    import random

    rnd = random.Random(20260917)
    side = 2200
    data = bytes(rnd.getrandbits(8) for _ in range(side * side * 3 // 64))
    # 위 난수 블록을 타일링해 side x side RGB 를 채운다 (난수 생성 비용 절감)
    need = side * side * 3
    payload = (data * (need // len(data) + 1))[:need]
    im = Image.frombytes("RGB", (side, side), payload)
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=0)
    raw = buf.getvalue()
    assert len(raw) > target_bytes, f"fixture too small: {len(raw)}"
    return raw


def _entry(name, ext, raw, media_type=None, is_image=True):
    return {
        "name": name,
        "ext": ext,
        "is_image": is_image,
        "readable": True,
        "media_type": media_type,
        "base64_data": base64.b64encode(raw).decode(),
    }


# ── 1. 네이티브 포맷 ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "fmt,ext,media_type",
    [("JPEG", ".jpg", "image/jpeg"), ("PNG", ".png", "image/png"), ("WEBP", ".webp", "image/webp")],
)
def test_native_formats_pass_through(fmt, ext, media_type):
    raw = _img_bytes(fmt)
    blocks = extract_image_blocks([_entry(f"a{ext}", ext, raw, media_type)])

    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[0]["source"]["media_type"] == media_type
    # 네이티브 경로는 원본을 그대로 실어 보낸다 (재인코딩 없음)
    assert base64.b64decode(blocks[0]["source"]["data"]) == raw


def test_block_key_structure_unchanged():
    blocks = extract_image_blocks([_entry("a.png", ".png", _img_bytes("PNG"), "image/png")])
    assert set(blocks[0].keys()) == {"type", "source"}
    assert set(blocks[0]["source"].keys()) == {"type", "media_type", "data"}


# ── 2. bmp → PNG 변환 ────────────────────────────────────────────────

def test_bmp_converted_to_png():
    raw = _img_bytes("BMP")
    blocks = extract_image_blocks([_entry("a.bmp", ".bmp", raw, "image/bmp")])

    assert len(blocks) == 1
    assert blocks[0]["source"]["media_type"] == "image/png"
    decoded = base64.b64decode(blocks[0]["source"]["data"])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"


def test_tiff_converted_to_png():
    blocks = extract_image_blocks([_entry("a.tiff", ".tiff", _img_bytes("TIFF"), None)])
    assert len(blocks) == 1
    assert blocks[0]["source"]["media_type"] == "image/png"


# ── 3. 5MB 초과 → 리샘플 ─────────────────────────────────────────────

def test_oversize_image_is_resized_not_dropped(caplog):
    raw = _noisy_png_bytes(6 * 1024 * 1024)
    original_size = len(raw)

    with caplog.at_level(logging.INFO, logger="app.core.document_context"):
        blocks = extract_image_blocks([_entry("big.png", ".png", raw, "image/png")])

    assert len(blocks) == 1, "초과 이미지는 버리지 않고 축소해야 한다"
    final = base64.b64decode(blocks[0]["source"]["data"])
    assert len(final) <= IMAGE_MAX_BYTES
    assert blocks[0]["source"]["media_type"] == "image/png"

    with Image.open(io.BytesIO(final)) as im:
        assert max(im.size) <= dc.VISION_MAX_DIMENSION

    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "resized=True" in log
    assert f"original_size={original_size}" in log
    assert f"final_size={len(final)}" in log


# ── 4. 미지원 포맷 건너뜀 ────────────────────────────────────────────

def test_heic_skipped_with_log(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.document_context"):
        blocks = extract_image_blocks([_entry("photo.heic", ".heic", b"\x00\x01ftypheic", "image/heic")])

    assert blocks == []
    assert "[unsupported_image_format: .heic]" in "\n".join(r.getMessage() for r in caplog.records)


def test_unsupported_does_not_block_siblings():
    entries = [
        _entry("photo.heic", ".heic", b"\x00\x01ftypheic", "image/heic"),
        _entry("ok.png", ".png", _img_bytes("PNG"), "image/png"),
    ]
    assert len(extract_image_blocks(entries)) == 1


# ── 5. attachments + extra_paths 중복 제거 ───────────────────────────

def test_duplicate_across_attachment_and_extra_path(tmp_path):
    raw = _img_bytes("PNG")
    path = tmp_path / "same.png"
    path.write_bytes(raw)

    blocks = extract_image_blocks(
        [_entry("same.png", ".png", raw, "image/png")],
        extra_paths=[str(path)],
    )
    assert len(blocks) == 1, "SHA-256 동일 이미지는 1건으로 합쳐야 한다"


def test_extra_paths_adds_distinct_image(tmp_path):
    path = tmp_path / "other.png"
    path.write_bytes(_img_bytes("PNG", color=(10, 220, 40)))

    blocks = extract_image_blocks(
        [_entry("a.png", ".png", _img_bytes("PNG"), "image/png")],
        extra_paths=[str(path)],
    )
    assert len(blocks) == 2


def test_extra_paths_none_keeps_legacy_behaviour():
    entries = [_entry("a.png", ".png", _img_bytes("PNG"), "image/png")]
    assert extract_image_blocks(entries) == extract_image_blocks(entries, extra_paths=None)


# ── 민감 경로 차단 ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["/etc/passwd", "/root/.ssh/id_rsa", "/proc/self/environ", "/run/secrets/x"])
def test_sensitive_paths_refused(bad, caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.document_context"):
        blocks = extract_image_blocks([], extra_paths=[bad])
    assert blocks == []
    assert "sensitive path refused" in "\n".join(r.getMessage() for r in caplog.records)


def test_missing_extra_path_is_skipped(tmp_path):
    assert extract_image_blocks([], extra_paths=[str(tmp_path / "nope.png")]) == []


# ── PDF document block ───────────────────────────────────────────────

def test_pdf_becomes_document_block(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% minimal fixture\n")

    blocks = extract_image_blocks([], extra_paths=[str(pdf)])
    assert len(blocks) == 1
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"


def test_oversize_pdf_skipped(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(dc, "PDF_MAX_BYTES", 16)
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 200)

    with caplog.at_level(logging.WARNING, logger="app.core.document_context"):
        blocks = extract_image_blocks([], extra_paths=[str(pdf)])
    assert blocks == []
    assert "pdf too large" in "\n".join(r.getMessage() for r in caplog.records)


# ── 6. call_llm_with_fallback(images=None) → 기존 동작 동일 ──────────

class _FakeUsage:
    input_tokens = 1
    output_tokens = 1
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _FakeResp:
    usage = _FakeUsage()

    def __init__(self, text="ok"):
        self.content = [type("B", (), {"text": text})()]


class _FakeRaw:
    headers = {}

    def __init__(self, resp):
        self._resp = resp

    def parse(self):
        return self._resp


def _install_fake_claude(monkeypatch, captured):
    from app.core import anthropic_client as ac

    class _Messages:
        def __init__(self):
            self.with_raw_response = self

        async def create(self, **kwargs):
            captured.append(kwargs)
            return _FakeRaw(_FakeResp())

    class _Client:
        def __init__(self):
            self.messages = _Messages()

    async def _tokens():
        return ["sk-ant-oat01-testtoken"]

    monkeypatch.setattr(ac, "get_oauth_tokens_async", _tokens)
    monkeypatch.setattr(ac, "create_anthropic_client", lambda *a, **k: _Client())
    monkeypatch.setattr(ac, "log_usage", lambda **k: None, raising=False)
    return ac


@pytest.mark.asyncio
async def test_call_llm_with_fallback_images_none_is_legacy(monkeypatch):
    captured = []
    ac = _install_fake_claude(monkeypatch, captured)

    out = await ac.call_llm_with_fallback("hello", model="claude-haiku-4-5-20251001")

    assert out == "ok"
    assert len(captured) == 1
    # images 미지정 → content 는 예전처럼 평문 문자열이어야 한다
    assert captured[0]["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_call_llm_with_fallback_images_promotes_content(monkeypatch):
    captured = []
    ac = _install_fake_claude(monkeypatch, captured)
    blocks = extract_image_blocks([_entry("a.png", ".png", _img_bytes("PNG"), "image/png")])

    out = await ac.call_llm_with_fallback("look", model="claude-haiku-4-5-20251001", images=blocks)

    assert out == "ok"
    content = captured[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"


def test_openai_fallback_converts_to_image_url():
    from app.core.anthropic_client import _to_openai_image_content

    blocks = extract_image_blocks([_entry("a.png", ".png", _img_bytes("PNG"), "image/png")])
    parts = _to_openai_image_content("look", blocks)

    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

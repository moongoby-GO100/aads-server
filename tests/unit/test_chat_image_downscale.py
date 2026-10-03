"""채팅 첨부 이미지 LLM 전송 전 축소·예산 (AADS-CHAT-IMAGE-DOWNSCALE-BEFORE-LLM)."""
from __future__ import annotations

import base64
import io
import os

from PIL import Image

from app.core import vision_budget as vb


def _noise_image(w: int, h: int, mode: str = "RGB") -> Image.Image:
    raw = os.urandom(w * h * len(mode))
    return Image.frombytes(mode, (w, h), raw)


def _block(img: Image.Image, fmt: str = "PNG") -> dict:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": f"image/{fmt.lower()}",
            "data": base64.b64encode(buf.getvalue()).decode(),
        },
    }


def _decode(block: dict) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))


def _total(blocks) -> int:
    return sum(len(b["source"]["data"]) for b in blocks)


def test_large_png_is_downscaled_to_1568_and_fits_budget():
    # 4000x2200 노이즈 PNG — 원본 base64 는 수 MB (실측 사고와 같은 규모)
    big = _block(_noise_image(4000, 2200))
    assert len(big["source"]["data"]) > 3_000_000

    out, notice = vb.fit_image_blocks([big], ["screen.png"])

    assert notice == ""
    assert len(out) == 1
    im = _decode(out[0])
    assert max(im.size) <= 1568
    assert out[0]["source"]["media_type"] == "image/jpeg"
    assert _total(out) <= vb.DEFAULT_BASE64_BUDGET
    # 입력 block 은 변형하지 않는다 (원본은 그대로)
    assert len(big["source"]["data"]) > 3_000_000


def test_alpha_image_is_reencoded_as_webp():
    rgba = _noise_image(2400, 1200, "RGBA")
    out, notice = vb.fit_image_blocks([_block(rgba)], ["a.png"])

    assert notice == ""
    assert out[0]["source"]["media_type"] == "image/webp"
    im = _decode(out[0])
    assert max(im.size) <= 1568
    assert "A" in im.getbands()


def test_small_image_passes_through_untouched():
    small = _block(Image.new("RGB", (320, 200), (10, 120, 200)))
    out, notice = vb.fit_image_blocks([small], ["small.png"])

    assert notice == ""
    assert out == [small]
    assert out[0]["source"]["data"] == small["source"]["data"]
    assert out[0]["source"]["media_type"] == "image/png"


def test_over_budget_excludes_last_images_with_notice():
    blocks = [_block(_noise_image(900, 700)) for _ in range(3)]
    names = ["one.png", "two.png", "three.png"]

    # 노이즈 900x700 JPEG 한 장도 이 예산에는 못 들어간다 → 전부 단계적 축소 후 뒤에서부터 제외
    out, notice = vb.fit_image_blocks(blocks, names, max_dim=1568, budget=60_000)

    assert _total(out) <= 60_000
    assert len(out) < 3
    assert notice.startswith(f"[이미지 {3 - len(out)}개는 크기 한도로 제외: ")
    assert notice.endswith("]")
    kept = len(out)
    for gone in names[kept:]:
        assert gone in notice
    for stay in names[:kept]:
        assert stay not in notice


def test_total_over_budget_degrades_before_excluding():
    # 한 장은 예산 안이지만 세 장 합계는 넘는다 → 품질을 낮춰 모두 담는다
    blocks = [_block(_noise_image(1200, 800)) for _ in range(3)]
    out, notice = vb.fit_image_blocks(blocks, budget=700_000)

    assert notice == ""
    assert len(out) == 3
    assert _total(out) <= 700_000


def test_budget_is_env_configurable(monkeypatch):
    monkeypatch.setenv("CHAT_VISION_BASE64_BUDGET", "1234")
    monkeypatch.setenv("CHAT_VISION_MAX_DIMENSION", "800")
    assert vb.base64_budget() == 1234
    assert vb.max_dimension() == 800
    monkeypatch.setenv("CHAT_VISION_BASE64_BUDGET", "not-a-number")
    assert vb.base64_budget() == vb.DEFAULT_BASE64_BUDGET


def test_pdf_document_block_is_passed_through():
    pdf = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "QUJD"}}
    img = _block(_noise_image(3000, 2000))
    out, notice = vb.fit_image_blocks([pdf, img])

    assert out[0] is pdf
    assert notice == ""
    assert max(_decode(out[1]).size) <= 1568


def test_unnamed_images_get_ordinal_labels_in_notice():
    blocks = [_block(_noise_image(900, 700)) for _ in range(2)]
    out, notice = vb.fit_image_blocks(blocks, budget=100)

    assert out == []
    assert "이미지 1" in notice and "이미지 2" in notice

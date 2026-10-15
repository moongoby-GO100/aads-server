"""계약서 도장 기하(서버판) — app/static/apps/obys/modules/company-seal.js 의 layout() 과 같은 계산.

화면은 SVG 로, 서명본 PDF 는 reportlab 벡터로 같은 좌표에 그린다. 어느 한쪽만 고치면 화면 도장과
PDF 도장이 달라지므로, tests/unit/test_obys_company_seal.py 가 두 구현의 좌표를 비교한다.
이미지 파일은 만들지 않는다.
"""
from __future__ import annotations

import math
import re
from typing import Any

SIZE = 200
CENTER = 100
OUTER_R = 96
INNER_R = 54
RING_MID_R = 75
DOT_ANGLE = -54
DOT_R = 4.5
RING_MAX_FS = 28
INNER_MAX_FS = 30
INNER_BOX = 68
CORP_TITLE = "대표이사인"
INK_RGB = (0xC1 / 255, 0x27 / 255, 0x2D / 255)
_CORP_PATTERN = re.compile(r"주식회사|\(주\)|㈜|유한회사|유한책임회사|합자회사|합명회사|법인")
_CORP_TYPES = {"corporation", "corporate", "법인", "법인사업자"}
_INDIVIDUAL_TYPES = {"individual", "개인", "개인사업자"}


def _round(value: float) -> float:
    return math.floor(value * 100 + 0.5) / 100


def _clean(value: Any) -> list[str]:
    return list(re.sub(r"\s+", "", str(value if value is not None else "")))


def kind_for(entity_type: Any, company_name: Any) -> str:
    text = str(entity_type if entity_type is not None else "").strip().lower()
    if text in _CORP_TYPES:
        return "corporation"
    if text in _INDIVIDUAL_TYPES:
        return "individual"
    return "corporation" if _CORP_PATTERN.search(str(company_name or "")) else "individual"


def layout(kind: str, company_name: Any, representative: Any) -> dict[str, Any] | None:
    """글자 크기·위치. 이름·회사명이 비면 None(도장 미표시). 좌표는 SVG 200x200 기준(y 아래로)."""
    kind = "corporation" if kind == "corporation" else "individual"
    company = _clean(company_name)
    rep = _clean(representative)
    ring = company if kind == "corporation" else (company or rep)
    inner = _clean(CORP_TITLE) if kind == "corporation" else ([*rep, *_clean("인")] if rep else [])
    if not ring or not inner:
        return None

    step = 360 / (len(ring) + 1)
    arc = RING_MID_R * (step * math.pi / 180)
    ring_fs = _round(min(RING_MAX_FS, arc * 0.8))
    ring_chars = [
        {
            "ch": ch,
            "angle": _round(DOT_ANGLE + step * (i + 1)),
            "x": CENTER,
            "y": _round(CENTER - RING_MID_R + ring_fs * 0.35),
            "fs": ring_fs,
        }
        for i, ch in enumerate(ring)
    ]

    right_count = math.ceil(len(inner) / 2)
    inner_fs = _round(min(INNER_MAX_FS, INNER_BOX / right_count))
    gap = _round(inner_fs * 1.05)
    inner_chars: list[dict[str, Any]] = []
    for column_index, column in enumerate((inner[:right_count], inner[right_count:])):
        x = _round(CENTER + (gap / 2 if column_index == 0 else -gap / 2))
        top = CENTER - (len(column) * inner_fs) / 2
        for row, ch in enumerate(column):
            inner_chars.append({"ch": ch, "x": x, "y": _round(top + row * inner_fs + inner_fs * 0.82), "fs": inner_fs})

    rad = DOT_ANGLE * math.pi / 180
    return {
        "kind": kind,
        "dot": {
            "angle": DOT_ANGLE,
            "r": DOT_R,
            "x": _round(CENTER + RING_MID_R * math.sin(rad)),
            "y": _round(CENTER - RING_MID_R * math.cos(rad)),
        },
        "ring": ring_chars,
        "inner": inner_chars,
    }


def draw_seal(canv: Any, plan: dict[str, Any], x: float, y: float, size: float, font: str) -> None:
    """reportlab canvas 에 도장을 벡터로 그린다. (x, y) 는 도장 정사각형의 왼쪽 아래, size 는 한 변."""
    scale = size / SIZE

    def px(value: float) -> float:
        return value * scale

    def flip(value: float) -> float:
        return (SIZE - value) * scale

    canv.saveState()
    try:
        canv.translate(x, y)
        canv.setStrokeColorRGB(*INK_RGB)
        canv.setFillColorRGB(*INK_RGB)
        canv.setLineWidth(3.2 * scale)
        canv.circle(px(CENTER), flip(CENTER), px(OUTER_R), stroke=1, fill=0)
        canv.setLineWidth(2.4 * scale)
        canv.circle(px(CENTER), flip(CENTER), px(INNER_R), stroke=1, fill=0)
        canv.circle(px(plan["dot"]["x"]), flip(plan["dot"]["y"]), px(DOT_R), stroke=0, fill=1)
        canv.setLineWidth(0.6 * scale)
        for char in plan["ring"]:
            canv.saveState()
            canv.translate(px(CENTER), flip(CENTER))
            canv.rotate(-char["angle"])
            _draw_char(canv, char["ch"], 0, -px(char["y"] - CENTER), px(char["fs"]), font)
            canv.restoreState()
        for char in plan["inner"]:
            _draw_char(canv, char["ch"], px(char["x"]), flip(char["y"]), px(char["fs"]), font)
    finally:
        canv.restoreState()


def _draw_char(canv: Any, ch: str, x: float, y: float, font_size: float, font: str) -> None:
    text = canv.beginText()
    text.setTextRenderMode(2)
    text.setFont(font, font_size)
    width = canv.stringWidth(ch, font, font_size)
    text.setTextOrigin(x - width / 2, y)
    text.textOut(ch)
    canv.drawText(text)

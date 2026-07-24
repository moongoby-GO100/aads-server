from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "app" / "static" / "apps" / "yeoljeong-finance" / "assets" / "prints"
DPI = 300
MM_PER_INCH = 25.4
FONT_PATH = Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")


def px(mm: float) -> int:
    return round(mm / MM_PER_INCH * DPI)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size=size)


def text_size(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=face)
    return box[2] - box[0], box[3] - box[1]


def centered(draw: ImageDraw.ImageDraw, y: int, text: str, face: ImageFont.ImageFont, fill: str) -> None:
    w, _ = text_size(draw, text, face)
    draw.text(((draw.im.size[0] - w) // 2, y), text, font=face, fill=fill)


def wrap(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in text.split():
        candidate = f"{current} {token}".strip()
        if text_size(draw, candidate, face)[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = token
    if current:
        lines.append(current)
    return lines


def rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], radius: int, fill: str, outline: str | None = None, width: int = 1) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def bowl(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float) -> None:
    bowl_w = int(980 * scale)
    bowl_h = int(360 * scale)
    draw.ellipse((cx - bowl_w // 2, cy - bowl_h // 2, cx + bowl_w // 2, cy + bowl_h // 2), fill="#fff8ea", outline="#24130c", width=max(8, int(18 * scale)))
    draw.ellipse((cx - int(410 * scale), cy - int(118 * scale), cx + int(410 * scale), cy + int(118 * scale)), fill="#cb2f24")
    draw.ellipse((cx - int(340 * scale), cy - int(88 * scale), cx + int(340 * scale), cy + int(88 * scale)), fill="#f15f2b")
    for i, color in enumerate(["#ffe3a1", "#fff4cb", "#8ab35a", "#f4c9a3", "#fff7dc"]):
        x = cx - int(260 * scale) + i * int(130 * scale)
        draw.ellipse((x, cy - int(92 * scale), x + int(160 * scale), cy + int(42 * scale)), fill=color)
    draw.arc((cx - int(390 * scale), cy - int(24 * scale), cx + int(390 * scale), cy + int(250 * scale)), 0, 180, fill="#24130c", width=max(5, int(12 * scale)))


def cold_noodle(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float) -> None:
    bowl_w = int(1120 * scale)
    bowl_h = int(540 * scale)
    draw.ellipse((cx - bowl_w // 2, cy - bowl_h // 2, cx + bowl_w // 2, cy + bowl_h // 2), fill="#f9fbff", outline="#17324d", width=max(8, int(18 * scale)))
    draw.ellipse((cx - int(470 * scale), cy - int(185 * scale), cx + int(470 * scale), cy + int(185 * scale)), fill="#c9eef7")
    for offset, color in [(-260, "#f7d79a"), (-90, "#fbf0c9"), (80, "#9bc86d"), (250, "#e64f35")]:
        draw.ellipse((cx + int(offset * scale), cy - int(160 * scale), cx + int((offset + 250) * scale), cy + int(80 * scale)), fill=color)
    for x in range(cx - int(360 * scale), cx + int(360 * scale), int(46 * scale)):
        draw.line((x, cy - int(60 * scale), x + int(220 * scale), cy + int(42 * scale)), fill="#fff8d7", width=max(3, int(7 * scale)))
    for x, y in [(cx - 240, cy - 180), (cx + 180, cy - 140), (cx + 330, cy - 25), (cx - 360, cy - 12)]:
        draw.rounded_rectangle((int(x * scale + cx * (1 - scale)), int(y * scale + cy * (1 - scale)), int((x + 68) * scale + cx * (1 - scale)), int((y + 44) * scale + cy * (1 - scale))), radius=max(5, int(10 * scale)), fill="#eaf8ff")


def perforation_marks(draw: ImageDraw.ImageDraw, w: int, h: int, safe: int, punch: int) -> None:
    draw.rectangle((safe, safe, w - safe, h - safe), outline="#33b36b", width=8)
    draw.rectangle((punch, punch, w - punch, h - punch), outline="#ffb020", width=5)
    r = px(4)
    gap = px(42)
    for x in range(punch + gap, w - punch, gap):
        for y in (punch // 2, h - punch // 2):
            draw.ellipse((x - r, y - r, x + r, y + r), fill="#ffb020")
    for y in range(punch + gap, h - punch, gap):
        for x in (punch // 2, w - punch // 2):
            draw.ellipse((x - r, y - r, x + r, y + r), fill="#ffb020")


def save_meta(name: str, image: Image.Image, physical_mm: tuple[int, int], role: str, notes: list[str]) -> dict[str, object]:
    path = OUT_DIR / name
    image.save(path, dpi=(DPI, DPI), optimize=True)
    return {
        "file": name,
        "url": f"/static/apps/yeoljeong-finance/assets/prints/{name}",
        "pixels": list(image.size),
        "dpi": DPI,
        "physical_mm": list(physical_mm),
        "role": role,
        "notes": notes,
    }


def create_pickup(name: str, physical_mm: tuple[int, int], title: str, subtitle: str, variant: str, safe_mm: int = 35, punch_mm: int = 18) -> dict[str, object]:
    w, h = px(physical_mm[0]), px(physical_mm[1])
    img = Image.new("RGB", (w, h), "#fff8ea")
    draw = ImageDraw.Draw(img)
    for y in range(h):
        ratio = y / max(1, h - 1)
        r = int(255 * (1 - ratio) + 238 * ratio)
        g = int(248 * (1 - ratio) + 72 * ratio)
        b = int(234 * (1 - ratio) + 45 * ratio)
        draw.line((0, y, w, y), fill=(r, g, b))
    draw.rectangle((0, 0, w, px(84)), fill="#15110d")
    centered(draw, px(25), "열정국밥", font(max(82, w // 29)), "#fff8ea")
    centered(draw, px(118), title, font(max(170, w // 11)), "#15110d")
    centered(draw, px(194), subtitle, font(max(58, w // 35)), "#4d2a19")
    bowl(draw, w // 2, int(h * 0.55), scale=w / 5200)
    bottom_h = px(104)
    draw.rectangle((0, h - bottom_h, w, h), fill="#15110d")
    centered(draw, h - bottom_h + px(22), "주문번호 확인 후 픽업대에서 받아가세요", font(max(54, w // 44)), "#fff8ea")
    centered(draw, h - bottom_h + px(56), "TAKE OUT · PICK-UP ZONE", font(max(42, w // 58)), "#f47b33")
    if variant == "perforation":
        perforation_marks(draw, w, h, px(safe_mm), px(punch_mm))
    return save_meta(
        name,
        img,
        physical_mm,
        "glass pickup poster",
        [
            "300DPI PNG",
            f"{safe_mm}mm safe area",
            f"{punch_mm}mm perforation caution guide",
            "B-1 상단 소형 보조이미지 제거 기준 반영" if variant == "b1-clean" else "유리 부착 타공 안전영역 표시",
        ],
    )


def create_noodle(name: str, physical_mm: tuple[int, int]) -> dict[str, object]:
    w, h = px(physical_mm[0]), px(physical_mm[1])
    img = Image.new("RGB", (w, h), "#eef8ff")
    draw = ImageDraw.Draw(img)
    for x in range(w):
        ratio = x / max(1, w - 1)
        r = int(238 * (1 - ratio) + 21 * ratio)
        g = int(248 * (1 - ratio) + 49 * ratio)
        b = int(255 * (1 - ratio) + 77 * ratio)
        draw.line((x, 0, x, h), fill=(r, g, b))
    draw.rectangle((0, 0, w, px(78)), fill="#132b45")
    centered(draw, px(23), "열정국밥", font(max(72, w // 30)), "#ffffff")
    centered(draw, px(115), "시원한 냉면", font(max(168, w // 11)), "#132b45")
    for y, line in enumerate(wrap(draw, "사진형 구성으로 메뉴 비주얼을 크게 배치한 실내용 B-2 시안", font(max(50, w // 48)), int(w * 0.82))):
        centered(draw, px(198) + y * px(22), line, font(max(50, w // 48)), "#2d526e")
    cold_noodle(draw, w // 2, int(h * 0.57), scale=w / 4700)
    rounded_rect(draw, (px(38), h - px(138), w - px(38), h - px(42)), px(12), "#ffffff", "#132b45", 5)
    centered(draw, h - px(110), "매장 식사 · 포장 주문 가능", font(max(55, w // 45)), "#132b45")
    centered(draw, h - px(72), "B-2 300DPI PRINT READY", font(max(38, w // 63)), "#3b718e")
    return save_meta(
        name,
        img,
        physical_mm,
        "cold noodle food visual poster",
        ["300DPI PNG", "냉면 사진 교체 요청을 반영한 대형 메뉴 비주얼형", "가까운 검수용 텍스트 최소화"],
    )


def create_page(metadata: list[dict[str, object]]) -> None:
    cards = "\n".join(
        f"""        <article class="asset-card">
          <img src="{item['url']}" alt="{item['role']} preview">
          <div class="asset-info">
            <h2>{item['file']}</h2>
            <p>{item['physical_mm'][0]}×{item['physical_mm'][1]}mm · {item['pixels'][0]}×{item['pixels'][1]}px · {item['dpi']}DPI</p>
            <ul>{''.join(f'<li>{note}</li>' for note in item['notes'])}</ul>
            <a href="{item['url']}" download>PNG 다운로드</a>
          </div>
        </article>"""
        for item in metadata
    )
    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>열정국밥 실내용 배너 300DPI 검수</title>
  <style>
    :root {{ color-scheme: light; --ink:#171a21; --muted:#606977; --line:#e4e8ee; --brand:#f26a2e; --dark:#15110d; --page:#f6f7f9; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; min-width:320px; font-family:Pretendard, "Noto Sans KR", system-ui, sans-serif; background:var(--page); color:var(--ink); }}
    header {{ padding:28px clamp(18px, 4vw, 48px) 18px; background:#fff; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0; font-size:clamp(22px, 3vw, 34px); }}
    header p {{ margin:8px 0 0; color:var(--muted); font-size:14px; line-height:1.6; }}
    main {{ width:min(1240px, 100%); margin:0 auto; padding:24px clamp(14px, 3vw, 28px) 48px; display:grid; gap:16px; }}
    .asset-card {{ display:grid; grid-template-columns:minmax(220px, 360px) minmax(0,1fr); gap:18px; align-items:center; padding:18px; background:#fff; border:1px solid var(--line); border-radius:8px; }}
    .asset-card img {{ width:100%; max-height:420px; object-fit:contain; background:#f2f3f5; border:1px solid var(--line); border-radius:6px; }}
    .asset-info h2 {{ margin:0; font-size:18px; overflow-wrap:anywhere; }}
    .asset-info p {{ margin:8px 0 12px; color:var(--muted); font-size:13px; }}
    ul {{ margin:0 0 14px; padding-left:18px; color:#394252; font-size:13px; line-height:1.65; }}
    a {{ display:inline-flex; min-height:40px; align-items:center; padding:0 14px; border-radius:7px; background:var(--dark); color:#fff; text-decoration:none; font-weight:800; font-size:13px; }}
    .note {{ padding:14px 16px; border:1px solid #f2d3bd; border-radius:8px; background:#fff7f0; color:#65331d; font-size:13px; line-height:1.6; }}
    @media (max-width: 720px) {{ .asset-card {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <header>
    <h1>열정국밥 실내용 배너 300DPI 검수</h1>
    <p>B-1 상단 소형 이미지 제거, B-2 냉면 비주얼 교체, INDOOR P4 유리 부착 타공 안전영역 기준을 분리 산출물로 정리했습니다.</p>
  </header>
  <main>
    <div class="note">표시된 미리보기는 브라우저 축소본입니다. 실제 인쇄 파일은 각 PNG 원본에 300DPI 메타데이터를 포함합니다.</div>
{cards}
  </main>
</body>
</html>
"""
    (OUT_DIR.parent.parent / "banners.html").write_text(html, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    metadata = [
        create_pickup(
            "indoor-b1-glass-pickup-clean-300dpi.png",
            (364, 515),
            "PICK-UP",
            "상단 소형 이미지 제거 · 중앙 메뉴 비주얼 집중",
            "b1-clean",
        ),
        create_noodle("indoor-b2-cold-noodle-visual-300dpi.png", (364, 515)),
        create_pickup(
            "indoor-p4-glass-pickup-perforation-safe-300dpi.png",
            (297, 420),
            "픽업존",
            "유리 부착 · 타공 안전영역 검수용",
            "perforation",
        ),
    ]
    (OUT_DIR / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    create_page(metadata)


if __name__ == "__main__":
    main()

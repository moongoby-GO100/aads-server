import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
PRINT_DIR = ROOT / "app" / "static" / "apps" / "yeoljeong-finance" / "assets" / "prints"
BANNERS_HTML = ROOT / "app" / "static" / "apps" / "yeoljeong-finance" / "banners.html"


def test_indoor_banner_manifest_lists_print_ready_pngs() -> None:
    manifest = json.loads((PRINT_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert [item["file"] for item in manifest] == [
        "indoor-b1-glass-pickup-clean-300dpi.png",
        "indoor-b2-cold-noodle-visual-300dpi.png",
        "indoor-p4-glass-pickup-perforation-safe-300dpi.png",
    ]
    assert all(item["dpi"] == 300 for item in manifest)
    assert manifest[0]["pixels"] == [4299, 6083]
    assert manifest[1]["pixels"] == [4299, 6083]
    assert manifest[2]["pixels"] == [3508, 4961]


def test_indoor_banner_pngs_have_300_dpi_metadata() -> None:
    for path in PRINT_DIR.glob("*-300dpi.png"):
        with Image.open(path) as image:
            dpi = image.info.get("dpi")
            assert dpi is not None
            assert round(dpi[0]) == 300
            assert round(dpi[1]) == 300


def test_indoor_banner_review_page_links_every_asset() -> None:
    html = BANNERS_HTML.read_text(encoding="utf-8")

    assert "열정국밥 실내용 배너 300DPI 검수" in html
    assert "B-1 상단 소형 이미지 제거" in html
    assert "B-2 냉면 비주얼 교체" in html
    assert "INDOOR P4 유리 부착 타공 안전영역" in html
    for path in PRINT_DIR.glob("*-300dpi.png"):
        assert path.name in html
        assert f'href="/static/apps/yeoljeong-finance/assets/prints/{path.name}"' in html

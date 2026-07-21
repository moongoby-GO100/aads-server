from pathlib import Path


HTML = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "static"
    / "apps"
    / "yeoljeong-finance"
    / "index.html"
).read_text(encoding="utf-8")


def test_contract_preview_has_a4_print_layout():
    assert "@page" in HTML
    assert "size: A4 portrait" in HTML
    assert 'id="contractPreviewModal"' in HTML
    assert 'id="contractPreviewCard"' in HTML
    assert "width: 210mm" in HTML
    assert "min-height: 297mm" in HTML


def test_print_action_uses_browser_print_dialog():
    assert 'els.printBtn.addEventListener("click", () => window.print())' in HTML

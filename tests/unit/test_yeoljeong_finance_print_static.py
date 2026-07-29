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


def test_integrations_detail_buttons_have_operational_drawer_and_api_ctas():
    assert 'id="integrationDetailModal"' in HTML
    assert 'data-integration-detail="sales-channel-connect"' in HTML
    assert 'data-integration-detail="bank-connect"' in HTML
    assert 'data-integration-detail="supplier-connect"' in HTML
    assert 'data-integration-detail="tax-connect"' in HTML
    assert 'data-integration-detail="receipt-upload"' in HTML
    assert 'data-integration-detail="credential-vault"' in HTML
    assert 'data-integration-detail="recommended-connectors"' in HTML
    assert 'data-integration-detail="pos-connect"' in HTML
    assert 'data-integration-detail="review-connect"' in HTML
    assert 'data-integration-detail="hr-connect"' in HTML
    assert 'data-integration-detail="pg-connect"' in HTML
    assert 'data-integration-preset="${escapeHtml(bankService)}"' in HTML
    assert 'data-sync-financial-integration="${escapeHtml(bankService)}"' in HTML
    assert 'data-open-import="${escapeHtml(bankService)}"' in HTML
    assert "/transactions/sync" in HTML
    assert "/transactions/import" in HTML

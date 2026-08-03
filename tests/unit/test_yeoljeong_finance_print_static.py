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
    assert 'title: "보안 보관 정책"' in HTML
    assert 'data-integration-detail="recommended-connectors"' in HTML
    assert 'data-integration-detail="pos-connect"' in HTML
    assert 'data-integration-detail="review-connect"' in HTML
    assert 'data-integration-detail="hr-connect"' in HTML
    assert 'data-integration-detail="pg-connect"' in HTML
    assert 'data-integration-connect-service="shinhan_business"' in HTML
    assert 'data-integration-connect-service="ibk_business"' in HTML
    assert 'data-integration-connect-form' in HTML
    assert 'data-sync-financial-integration' in HTML
    assert 'data-open-import="${escapeHtml(service)}"' in HTML
    assert "/transactions/sync" in HTML
    assert "/transactions/import" in HTML


def test_integration_add_opens_mockup_setup_form_without_extra_fields():
    connect_spec = HTML.split("connect: {", 1)[1].split('"connect-form": {', 1)[0]
    form_html = HTML.split("function integrationConnectFormHtml", 1)[1].split("function integrationAddLandingHtml", 1)[0]

    assert "body: integrationConnectFormHtml" in connect_spec
    assert "integrationAddLandingHtml()" not in connect_spec
    assert "연동 설정 페이지" in form_html
    assert "integration-preset-strip" in form_html
    assert "credential-grid" in form_html
    assert "계좌/가맹점번호" in form_html
    assert "계좌비밀번호/API Secret" in form_html
    assert "사업자등록번호" in form_html
    assert 'name="passwordConfirm"' not in form_html
    assert 'name="authOwner"' not in form_html
    assert 'name="mfaMethod"' not in form_html
    assert 'name="oneTimePassword"' not in form_html
    assert 'name="credentialExpiresAt"' not in form_html

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
    assert 'presetButton("shinhan_business"' in HTML
    assert 'presetButton("ibk_business"' in HTML
    assert 'data-integration-connect-form' in HTML
    assert 'data-sync-financial-integration' in HTML
    assert 'data-open-import="${escapeHtml(service)}"' in HTML
    assert "/transactions/sync" in HTML
    assert "/transactions/import" in HTML


def test_integration_add_opens_service_specific_setup_forms():
    connect_spec = HTML.split("connect: {", 1)[1].split('"connect-form": {', 1)[0]
    edit_spec = HTML.split('"edit-form": {', 1)[1].split('"sales-channel-connect": {', 1)[0]
    form_html = HTML.split("function integrationConnectFormHtml", 1)[1].split("function integrationAddLandingHtml", 1)[0]
    landing_html = HTML.split("function integrationAddLandingHtml", 1)[1].split("function integrationDetailSpec", 1)[0]

    assert "body: integrationAddLandingHtml()" in connect_spec
    assert "먼저 연동 유형을 선택" in connect_spec
    assert 'title: "연동 설정 수정"' in edit_spec
    assert "연동 설정 페이지" in form_html
    assert "integration-edit-banner" in form_html
    assert "integration-preset-strip" in form_html
    assert "credential-grid" in form_html
    assert "판매채널 추가" in landing_html
    assert "은행 계좌 연결" in landing_html
    assert "카드/PG 리포트" in landing_html
    assert "매입처 등록" in landing_html
    assert "기타 매입처 연동" in landing_html
    assert "플랫폼 매장코드" in form_html
    assert "2차 인증 수단" in form_html
    assert '["browser-automation", "브라우저 자동화"]' in form_html
    assert "정산 CSV 업로드 대기열" in form_html
    assert "조회 계좌번호" in form_html
    assert "계좌비밀번호" in form_html
    assert "사업자등록번호" in form_html
    assert "거래명세서 OCR" in form_html
    assert "공동/금융인증서 비밀번호" in form_html
    assert "if (isSales) return [" in form_html
    assert "if (isBank) return [" in form_html
    assert "data-edit-integration" in HTML
    assert "function openIntegrationEdit" in HTML
    assert 'openIntegrationDetail("edit-form", service)' in HTML
    assert 'form.classList.add("is-editing")' in HTML
    assert "editIntegrationId" in HTML
    assert 'name="passwordConfirm"' not in form_html
    assert "rerenderIntegrationConnectForm(event.target.value, modalForm)" in HTML
    assert "rerenderIntegrationConnectForm(service, form)" in HTML

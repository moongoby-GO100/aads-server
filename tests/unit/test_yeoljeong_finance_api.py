import base64
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, FastAPI, UploadFile
from fastapi.testclient import TestClient

from app.api import yeoljeong_finance as api


def test_join_request_accepts_contract_autofill_profile():
    payload = api.JoinRequestCreate(
        name="가입 직원", email="employee@example.com", branch="중화점",
        phone="010-1234-5678", address="서울특별시 중랑구",
        birth_date="1990-01-01", nationality="대한민국",
    )
    assert payload.address == "서울특별시 중랑구"
    assert payload.birth_date == "1990-01-01"


def _disable_finance_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.mark.asyncio
async def test_upload_onboarding_document_awaits_async_service(monkeypatch):
    expected = {"id": "doc-1", "status": "uploaded"}

    async def fake_save_onboarding_document(**kwargs):
        assert kwargs["employee_email"] == "employee@example.com"
        assert kwargs["document_type"] == "bankbook"
        return expected

    monkeypatch.setattr(api.svc, "save_onboarding_document", fake_save_onboarding_document)
    upload = UploadFile(filename="bankbook.pdf", file=BytesIO(b"pdf"))

    result = await api.upload_onboarding_document(
        employee_name="직원",
        employee_email="employee@example.com",
        branch="중화점",
        document_type="bankbook",
        issue_date="2026-07-21",
        memo="",
        file=upload,
        current_user={"email": "owner@example.com", "is_admin": True},
    )

    assert result == {"document": expected}


@pytest.mark.asyncio
async def test_list_onboarding_documents_passes_business_scope(monkeypatch):
    def fake_list(current_user, business_id):
        assert current_user["is_admin"] is True
        assert business_id == "biz-junghwa"
        return [{"id": "doc-1"}]

    monkeypatch.setattr(api.svc, "list_onboarding_documents", fake_list)

    result = await api.list_onboarding_documents(
        business_id="biz-junghwa",
        current_user={"email": "owner@example.com", "is_admin": True},
    )

    assert result == {"documents": [{"id": "doc-1"}]}


def test_employee_signature_http_flow_records_authenticated_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(api.svc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api.svc, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(api.svc, "_run_db", _disable_finance_db)
    employee = {"email": "member@example.com", "is_admin": False}
    admin = {"email": "owner@example.com", "is_admin": True}
    api.svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": employee["email"],
        "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
        "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])
    saved = api.svc.save_contract({
        "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }, admin)
    requested = api.svc.request_contract_signature(saved["id"], admin)
    token = requested["sign_token"]

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: employee
    client = TestClient(app)
    viewed = client.get(f"/yeoljeong-finance/contracts/signing/{token}")
    assert viewed.status_code == 200
    png = b"\x89PNG\r\n\x1a\n" + (b"http-signature" * 16)
    signed = client.post(
        "/yeoljeong-finance/contracts/signing",
        headers={"x-forwarded-for": "203.0.113.22", "user-agent": "signature-e2e"},
        json={
            "token": token,
            "signer_name": "가입 직원",
            "consent": True,
            "consent_version": "yeoljeong-contract-sign-v1",
            "signature_data_uri": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        },
    )

    assert signed.status_code == 200
    contract = signed.json()["contract"]
    assert contract["status"] == "signed"
    assert contract["signature_audit"]["authenticated_email"] == employee["email"]
    assert contract["signature_audit"]["client_ip"] == "203.0.113.22"
    assert contract["signature_audit"]["user_agent"] == "signature-e2e"
    assert "sign_token" not in contract


def test_contract_preview_is_a4_modal():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert 'id="contractPreviewModal"' in html
    assert 'id="contractPreviewModalPaper"' in html
    assert "width: 210mm" in html
    assert "min-height: 297mm" in html
    assert "openContractPreviewModal(contract, \"저장 계약서 기준\")" in html
    assert "function contractPreviewWithCurrentReferences(contract)" in html
    assert 'String(source.status || "draft") !== "draft"' in html
    assert 'fillBlank("employeePhone", "employee_phone", employee.phone || "")' in html
    assert 'fillBlank("employerRegistrationNo", "employer_registration_no", business.registrationNo || "")' in html
    assert "최신양식 v2026.07.23" in html
    assert "체결 당시 저장본" in html


def test_delivery_integration_normalization_preserves_selected_business_scope():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    normalizer = html.split("function normalizeMiaBusinessLinks(next)", 1)[1].split("function mergeSettings", 1)[0]

    assert "branch?.businessId" in normalizer
    assert "if (!item.branch) item.branch = MIA_BRANCH_NAME;" in normalizer
    assert "item.branch = MIA_BRANCH_NAME;" not in normalizer.replace(
        "if (!item.branch) item.branch = MIA_BRANCH_NAME;",
        "",
    )


def test_integration_accounts_use_resolved_auth_token_and_session_first_load():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert "function serverAuthToken()" in html
    assert "localStorage.getItem(FB_ACCESS_TOKEN_KEY)" in html
    assert "cookieValue(SERVER_AUTH_TOKEN_KEY)" in html
    assert "const token = serverAuthToken();" in html
    loader = html.split("function ensureServerAccountsForIntegrations()", 1)[1].split("function integrationMatchesSyncSummary", 1)[0]
    assert "refreshFinanceSession()" in loader
    assert "return refreshServerAccounts();" in loader


def test_import_delivery_portal_text_persists_pc_parsed_baemin_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(api.svc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api.svc, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(api.svc, "EVIDENCE_UPLOAD_DIR", tmp_path / "uploads" / "evidence")
    monkeypatch.setattr(api.svc, "_run_db", _disable_finance_db)
    html = """
    <table>
      <tr><th>주문일</th><th>주문번호</th><th>결제금액</th><th>배달팁</th><th>주문상태</th></tr>
      <tr><td>2026.08.03</td><td>B-1001</td><td>31,000원</td><td>3,000원</td><td>완료</td></tr>
    </table>
    """

    result = api.svc.import_delivery_portal_text(
        {
            "service": "baemin",
            "record_type": "sales",
            "source_text": html,
            "filename": "baemin-sales.html",
            "business_id": "biz-junghwa",
            "branch": "중화점",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["totals"]["sales"] == 1
    assert result["sales"][0]["order_id"] == "B-1001"
    assert result["sales"][0]["gross_amount"] == 31000
    assert api.svc._read("delivery_collection_status")[0]["diagnostics"]["collection_mode"] == "pc-browser-parse"


@pytest.mark.asyncio
async def test_account_upsert_runs_financial_sync_when_auto_sync_enabled(monkeypatch):
    saved_account = {
        "id": "acct-shinhan",
        "service": "shinhan_business",
        "business_id": "biz-junghwa",
        "branch": "중화점",
    }
    sync_result = {"summary": [{"service": "shinhan_business", "status": "connector_not_configured"}]}

    def fake_upsert_account(payload, current_user):
        assert payload["auto_sync"] is True
        assert payload["service"] == "shinhan_business"
        return saved_account

    def fake_sync_financial_transactions(payload, current_user):
        assert payload == {
            "services": ["shinhan_business"],
            "account_id": "acct-shinhan",
            "business_id": "biz-junghwa",
            "branch": "중화점",
        }
        return sync_result

    monkeypatch.setattr(api.svc, "upsert_account", fake_upsert_account)
    monkeypatch.setattr(api.svc, "sync_financial_transactions", fake_sync_financial_transactions)

    payload = api.AccountUpsertPayload(
        service="shinhan_business",
        username="quick-user",
        password="login-secret",
        account_no="110123456789",
        account_password="4321",
        business_registration_no="7108604499",
        business_id="biz-junghwa",
        branch="중화점",
        collection_mode="bank-quick-service",
        auto_sync=True,
    )

    result = await api.upsert_account(payload, BackgroundTasks(), {"email": "owner@example.com", "is_admin": True})

    assert result == {"account": saved_account, "sync": sync_result}


@pytest.mark.asyncio
async def test_account_upsert_queues_delivery_sync_when_auto_sync_enabled(monkeypatch):
    saved_account = {
        "id": "acct-baemin",
        "service": "baemin",
        "business_id": "biz-junghwa",
        "branch": "중화점",
    }
    sync_result = {
        "queued": True,
        "job_id": "delivery-sync-test",
        "queued_run_ids": {"baemin": "run-baemin"},
        "summary": [{"service": "baemin", "status": "queued"}],
    }
    background_payloads = []

    def fake_upsert_account(payload, current_user):
        assert payload["auto_sync"] is True
        assert payload["service"] == "baemin"
        return saved_account

    def fake_queue_delivery_sync(payload, current_user):
        assert payload == {
            "services": ["baemin"],
            "account_id": "acct-baemin",
            "business_id": "biz-junghwa",
            "branch": "중화점",
        }
        return sync_result

    def fake_start_background(payload, current_user):
        background_payloads.append((payload, current_user))

    monkeypatch.setattr(api.svc, "upsert_account", fake_upsert_account)
    monkeypatch.setattr(api.svc, "queue_delivery_sync", fake_queue_delivery_sync)
    monkeypatch.setattr(api, "_start_delivery_sync_background", fake_start_background)

    payload = api.AccountUpsertPayload(
        service="baemin",
        username="baemin-user",
        password="login-secret",
        business_id="biz-junghwa",
        branch="중화점",
        collection_mode="browser-automation",
        auto_sync=True,
    )

    background_tasks = BackgroundTasks()
    result = await api.upsert_account(payload, background_tasks, {"email": "owner@example.com", "is_admin": True})

    assert result == {"account": saved_account, "sync": sync_result}
    assert background_tasks.tasks == []
    assert background_payloads == [(
        {
            "services": ["baemin"],
            "account_id": "acct-baemin",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "sync_job_id": "delivery-sync-test",
            "queued_run_ids": {"baemin": "run-baemin"},
        },
        {"email": "owner@example.com", "is_admin": True},
    )]


@pytest.mark.asyncio
async def test_sync_delivery_background_returns_after_queueing(monkeypatch):
    sync_result = {
        "queued": True,
        "job_id": "delivery-sync-test",
        "queued_run_ids": {"baemin": "run-baemin", "yogiyo": "run-yogiyo"},
        "summary": [{"service": "baemin", "status": "queued"}],
    }
    background_payloads = []

    def fake_queue_delivery_sync(payload, current_user):
        assert payload["background"] is True
        assert payload["services"] == ["baemin", "yogiyo"]
        return sync_result

    def fake_start_background(payload, current_user):
        background_payloads.append((payload, current_user))

    monkeypatch.setattr(api.svc, "queue_delivery_sync", fake_queue_delivery_sync)
    monkeypatch.setattr(api, "_start_delivery_sync_background", fake_start_background)

    payload = api.SyncPayload(
        services=["baemin", "yogiyo"],
        business_id="biz-junghwa",
        branch="중화점",
        background=True,
    )
    background_tasks = BackgroundTasks()
    result = await api.sync_delivery(payload, background_tasks, {"email": "owner@example.com", "is_admin": True})

    assert result == sync_result
    assert background_tasks.tasks == []
    assert len(background_payloads) == 1
    background_payload, background_user = background_payloads[0]
    assert background_user == {"email": "owner@example.com", "is_admin": True}
    assert background_payload["services"] == ["baemin", "yogiyo"]
    assert background_payload["business_id"] == "biz-junghwa"
    assert background_payload["branch"] == "중화점"
    assert background_payload["background"] is False
    assert background_payload["sync_job_id"] == "delivery-sync-test"
    assert background_payload["queued_run_ids"] == {"baemin": "run-baemin", "yogiyo": "run-yogiyo"}


@pytest.mark.asyncio
async def test_sync_delivery_preserves_baemin_full_backfill_options(monkeypatch):
    captured = {}

    def fake_sync_delivery(payload, current_user):
        captured.update(payload)
        return {"summary": []}

    monkeypatch.setattr(api.svc, "sync_delivery", fake_sync_delivery)

    payload = api.SyncPayload(
        services=["baemin"],
        business_id="all",
        branch="전체",
        all_businesses=True,
        mode="full_backfill",
        date_from="2026-01-01",
        date_to="2026-08-25",
        max_orders=200,
        max_reviews=150,
        checkpoint={"last_order_no": "T2FP00000XZV"},
    )
    result = await api.sync_delivery(
        payload,
        BackgroundTasks(),
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result == {"summary": []}
    assert captured["all_businesses"] is True
    assert captured["mode"] == "full_backfill"
    assert captured["max_orders"] == 200
    assert captured["max_reviews"] == 150
    assert captured["checkpoint"] == {"last_order_no": "T2FP00000XZV"}


def test_contract_editor_uses_safe_classification_and_locks_signed_records():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    assert 'wage: "12000"' not in html
    assert "function syncContractClassification()" in html
    assert 'els.contractForm.employmentTaxType.value = "freelancer_33"' in html
    assert 'els.contractForm.wageType.value = "case_fee"' in html
    assert "function validateContractDraft(draft)" in html
    assert '<select name="employeeRequestId" required>' in html
    assert "function applyEmployeeToForm(form, employee, overwrite = true)" in html
    assert 'employeeAddress: employee.address || ""' in html
    assert 'employeePhone: employee.phone || ""' in html
    assert "employeeBirthDate: employee.birth_date || employee.birthDate || \"\"" in html
    assert "서명본 수정·삭제 잠금" in html
    assert 'contractClause("용역 기간 및 장소"' in html
    assert 'contractClause("용역비 및 정산", `${wageLine}.' in html


def test_execution_contract_has_complete_worker_identity_and_no_editor_notice():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    for field in (
        'name="employeeAddress"', 'name="employeePhone"', 'name="employeeBirthDate"',
        'name="employeeNationality"', 'name="minorGuardianName"',
        'name="minorGuardianConsent"', 'name="dailyWorkSchedule"', 'name="bankName"',
        'name="bankAccountHolder"', 'name="bankAccountMasked"',
        'name="healthCertificateValidUntil"', 'name="onboardingDocumentSummary"',
    ):
        assert field in html
    preview = html.split("function contractPreviewHtml(contract)", 1)[1].split("function updateContractPreview", 1)[0]
    assert 'class="identity-table"' in preview
    assert 'aria-label="계약 당사자 인적사항"' in preview
    assert "생년월일·국적" in preview
    assert "급여계좌" in preview
    assert "입사서류 확인" in preview
    assert "보건증 유효기한" in preview
    assert "고용노동부 표준근로계약서의 필수 기재 축" not in preview
    assert "2026 최저임금 자동점검" not in preview


def test_employee_signup_collects_contract_autofill_profile():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    assert html.count('name="birthDate"') >= 3
    assert html.count('name="address"') >= 3
    assert html.count('name="nationality"') >= 3
    assert "birth_date: birthDate" in html
    assert "employee_birth_date: next.employeeBirthDate" in html
    assert "applyEmployeeToForm(els.contractForm, selectedApprovedEmployee(employeeRequestId, els.contractForm), false)" in html
    assert 'employerPhone: business.phone || ""' in html


def test_employee_auth_gate_prioritizes_login_but_keeps_employee_signup_available():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    gate = html.split('<section id="authGate"', 1)[1].split('<section id="appFilters"', 1)[0]

    assert '<h2 id="authGateTitle" class="panel-title">운영관리 로그인</h2>' in gate
    assert '<form id="gateLoginForm" class="panel-body">' in gate
    assert '<form id="gateSignupForm" class="panel-body hidden">' in gate
    assert '<form id="gateInviteAcceptForm" class="panel-body hidden">' in gate
    assert '<button id="openSignupFromGateBtn" type="button">직원 회원가입</button>' in gate
    assert "회원가입 후 입사서류 등록" in gate

    signup_function = html.split("async function signupToServer(formData)", 1)[1].split(
        "async function submitEmployeeSignupJoinRequest", 1
    )[0]
    assert 'const accountType = String(formData.accountType || "employee").trim();' in signup_function
    assert 'if (accountType === "employee")' in signup_function
    assert "joinRequest = await submitEmployeeSignupJoinRequest" in signup_function
    assert 'setView("onboarding")' in signup_function
    assert "직원 회원가입과 가입요청이 완료됐습니다. 입사서류를 등록하십시오." in signup_function


def test_member_permission_levels_are_visible_in_audit_view():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    audit = html.split('<section id="auditView"', 1)[1].split('<section id="loginModal"', 1)[0]

    assert "회원 권한 구분 (5단계)" in audit
    assert 'data-level="owner"' in audit
    assert 'data-level="admin"' in audit
    assert 'data-level="employee"' in audit
    assert 'data-level="employee_pending"' in audit
    assert 'data-level="employee_rejected"' in audit
    assert "currentMemberLevelNote" in audit
    assert "document.querySelectorAll(\".member-level\")" in html


def test_unni_recipe_redirect_restores_fb_cookie_for_existing_login():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    token_resolver = html.split("function serverAuthToken()", 1)[1].split("function apiHeaders()", 1)[0]

    assert "function syncServerAuthCookieFromStorage()" in html
    assert "localStorage.getItem(SERVER_AUTH_TOKEN_KEY)" in token_resolver
    assert "localStorage.getItem(FB_ACCESS_TOKEN_KEY)" in token_resolver
    assert "cookieValue(SERVER_AUTH_TOKEN_KEY)" in token_resolver
    assert "cookieValue(FB_ACCESS_TOKEN_KEY)" in token_resolver
    assert "SameSite=Lax${secure}" in html
    assert "const rememberedRecipeRedirect = rememberPostLoginRedirectFromUrl();" in html
    assert "syncServerAuthCookieFromStorage();" in html
    assert "if (rememberedRecipeRedirect && hasServerAuth() && followPostLoginRedirect()) return;" in html


def test_contract_signing_requires_employee_consent_and_drawn_signature():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert 'id="contractSignModal"' in html
    assert 'id="contractSignatureCanvas"' in html
    assert 'name="consent" type="checkbox" required' in html
    assert "function submitContractSignature(event)" in html
    assert 'consent_version: "yeoljeong-contract-sign-v1"' in html
    assert "signature_data_uri: els.contractSignatureCanvas.toDataURL" in html
    assert "관리자는 직원 대신 서명할 수 없습니다" in html
    manager_actions = html.split('const actions = managerMode ? `', 1)[1].split('` : (employeeMode', 1)[0]
    assert "data-sign-contract" not in manager_actions


def test_onboarding_open_uses_authenticated_file_preview_modal():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert 'id="filePreviewModal"' in html
    assert 'id="filePreviewContent"' in html
    assert "async function downloadOnboardingDocument(documentId)" in html
    assert "headers: apiAuthOnlyHeaders()" in html
    assert "URL.createObjectURL(blob)" in html
    assert "window.open(`/api/v1/yeoljeong-finance/onboarding/documents/" not in html


def test_bank_quick_service_ui_collects_required_vault_fields():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert '<option value="bank-quick-service">은행 간편/빠른조회</option>' in html
    assert 'name="accountNo"' in html
    assert 'name="accountPassword" type="password"' in html
    assert 'name="businessRegistrationNo"' in html
    assert 'account_no: data.accountNo || ""' in html
    assert 'account_password: data.accountPassword || ""' in html
    assert 'business_registration_no: data.businessRegistrationNo || ""' in html
    assert "pendingAutoSync = result.sync || null" in html
    assert "? applySyncPayload(pendingAutoSync)" in html
    assert ": applyFinancialSyncPayload(pendingAutoSync)" in html
    assert "if (result.sync) applySyncPayload(result.sync)" not in html
    assert "신한은행 간편서비스 계좌조회 기준" in html
    assert "IBK기업은행 빠른서비스 계좌조회 기준" in html
    assert "function csvDelimiter(text)" in html


def test_pdf_preview_does_not_sandbox_chrome_pdf_viewer():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    pdf_branch = html.split('contentType === "application/pdf"', 1)[1].split(
        'contentType.startsWith("text/")', 1
    )[0]
    text_branch = html.split('contentType.startsWith("text/")', 1)[1].split(
        "} else {", 1
    )[0]

    assert 'frame.setAttribute("sandbox", "")' not in pdf_branch
    assert 'frame.setAttribute("sandbox", "")' in text_branch


def test_bank_account_and_ledger_http_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(api.svc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api.svc, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(api.svc, "_run_db", _disable_finance_db)
    admin = {"email": "owner@example.com", "is_admin": True}

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: admin
    client = TestClient(app)

    created = client.post(
        "/yeoljeong-finance/bank-accounts",
        json={
            "business_id": "biz-mia",
            "branch_id": "branch-gangbuk-mia",
            "bank_code": "088",
            "bank_name": "신한은행",
            "account_number": "110-987-654321",
            "account_holder": "최미미",
            "account_alias": "미아점 주계좌",
            "connection_type": "mock",
            "status": "active",
        },
    )
    assert created.status_code == 200
    account = created.json()["bank_account"]
    assert account["account_number_masked"].endswith("4321")
    assert "account_number" not in account

    listed = client.get("/yeoljeong-finance/bank-accounts", params={"business_id": "biz-mia"})
    assert listed.status_code == 200
    assert len(listed.json()["bank_accounts"]) == 1

    patched = client.patch(
        f"/yeoljeong-finance/bank-accounts/{account['id']}",
        json={"status": "paused"},
    )
    assert patched.status_code == 200
    assert patched.json()["bank_account"]["status"] == "paused"

    imported = client.post(
        "/yeoljeong-finance/bank-transactions",
        json={
            "business_id": "biz-mia",
            "bank_account_id": account["id"],
            "transactions": [
                {"occurred_at": "2026-08-01", "direction": "in", "amount": 90000, "counterparty": "정산"},
                {"occurred_at": "2026-08-03", "direction": "out", "amount": 30000, "counterparty": "식자재"},
            ],
        },
    )
    assert imported.status_code == 200
    assert imported.json()["import"]["imported_rows"] == 2

    txns = client.get("/yeoljeong-finance/bank-transactions", params={"direction": "in"})
    assert txns.status_code == 200
    assert len(txns.json()["bank_transactions"]) == 1

    summary = client.get("/yeoljeong-finance/bank-summary", params={"business_id": "biz-mia"})
    assert summary.status_code == 200
    body = summary.json()
    assert body["totals"]["net"] == 60000
    assert body["totals"]["transaction_count"] == 2

    csv_import = client.post(
        "/yeoljeong-finance/bank-transactions/import",
        json={
            "business_id": "biz-mia",
            "branch_id": "branch-gangbuk-mia",
            "bank_account_id": account["id"],
            "csv_text": "거래일자,적요,입금액,출금액\n2026-08-04,카드정산,50000,\n",
            "filename": "bank.csv",
        },
    )
    assert csv_import.status_code == 200
    assert csv_import.json()["import"]["imported_rows"] == 1


def test_bank_account_rejects_extra_sensitive_field(tmp_path, monkeypatch):
    monkeypatch.setattr(api.svc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api.svc, "_run_db", _disable_finance_db)
    admin = {"email": "owner@example.com", "is_admin": True}

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: admin
    client = TestClient(app)

    resp = client.post(
        "/yeoljeong-finance/bank-accounts",
        json={"business_id": "biz-mia", "password": "should-not-be-allowed"},
    )
    # extra="forbid" blocks credential-shaped fields at the schema boundary.
    assert resp.status_code == 422

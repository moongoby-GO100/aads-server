import base64
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

from app.api import yeoljeong_finance as api


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


def test_contract_editor_uses_safe_classification_and_locks_signed_records():
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "yeoljeong-finance" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    assert 'wage: "12000"' not in html
    assert "function syncContractClassification()" in html
    assert 'els.contractForm.employmentTaxType.value = "freelancer_33"' in html
    assert 'els.contractForm.wageType.value = "case_fee"' in html
    assert "function validateContractDraft(draft)" in html
    assert '<select name="employeeRequestId" required>' in html
    assert 'form.employeeAddress.value = employee.address || ""' in html
    assert "서명본 수정·삭제 잠금" in html
    assert 'contractClause("용역 기간 및 장소"' in html
    assert 'contractClause("용역비 및 정산", `${wageLine}.' in html


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

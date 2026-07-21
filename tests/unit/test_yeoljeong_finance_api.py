from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from app.api import yeoljeong_finance as api


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

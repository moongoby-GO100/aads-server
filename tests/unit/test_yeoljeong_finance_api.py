from io import BytesIO

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

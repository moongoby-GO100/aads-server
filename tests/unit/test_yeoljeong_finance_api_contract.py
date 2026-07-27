from io import BytesIO

import pytest
from fastapi import UploadFile
from pydantic import ValidationError

from app.api import yeoljeong_finance
from app.api.yeoljeong_finance import AccountUpsertPayload


def test_account_password_is_write_only_and_hidden_from_repr():
    schema = AccountUpsertPayload.model_json_schema()
    payload = AccountUpsertPayload(
        service="baemin",
        username="owner",
        password="secret",
        account_no="110123456789",
        account_password="1234",
        business_registration_no="1234567890",
    )

    assert schema["properties"]["password"]["writeOnly"] is True
    assert schema["properties"]["account_no"]["writeOnly"] is True
    assert schema["properties"]["account_password"]["writeOnly"] is True
    assert schema["properties"]["business_registration_no"]["writeOnly"] is True
    assert "password=" not in repr(payload)
    assert "account_no=" not in repr(payload)
    assert "account_password=" not in repr(payload)
    assert "business_registration_no=" not in repr(payload)


def test_account_payload_rejects_secret_field_injection():
    with pytest.raises(ValidationError):
        AccountUpsertPayload(
            service="baemin",
            username="owner",
            password_enc="attacker-controlled-ciphertext",
        )


@pytest.mark.asyncio
async def test_onboarding_upload_awaits_async_service(monkeypatch):
    async def fake_save_onboarding_document(**kwargs):
        assert isinstance(kwargs["upload"], UploadFile)
        return {"id": "doc-e2e", "status": "pending"}

    monkeypatch.setattr(
        yeoljeong_finance.svc,
        "save_onboarding_document",
        fake_save_onboarding_document,
    )
    result = await yeoljeong_finance.upload_onboarding_document(
        employee_name="테스트 직원",
        employee_email="employee@example.com",
        branch="열정국밥_미아점",
        document_type="resident_registration",
        issue_date="2026-07-21",
        memo="E2E",
        file=UploadFile(filename="test.txt", file=BytesIO(b"test")),
        current_user={"email": "admin@example.com", "is_admin": True},
    )

    assert result == {"document": {"id": "doc-e2e", "status": "pending"}}

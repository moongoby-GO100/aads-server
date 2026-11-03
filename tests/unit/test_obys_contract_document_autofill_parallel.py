"""서류 프로필의 회사 및 고용 식별 회귀."""
from app.services import yeoljeong_finance_service as svc


def test_profile_excludes_other_employment_and_company():
    base = {"status": "uploaded", "employee_email": "fixture@example.test",
            "employee_request_id": "join-1", "business_id": "biz-mia",
            "extracted_fields": {"address": "fixture address"}}
    rows = [dict(base, employee_request_id="join-2"), dict(base, business_id="biz-other")]
    profile = svc._employee_onboarding_profile(rows, employee_email=base["employee_email"],
                                             employee_request_id="join-1", business_id="biz-mia")
    assert not profile.get("address")
    profile = svc._employee_onboarding_profile([base], employee_email=base["employee_email"],
                                             employee_request_id="join-1", business_id="biz-mia")
    assert profile["address"] == "fixture address"


def test_profile_never_links_by_name_or_deleted_document():
    rows = [{"employee_name": "fixture", "status": "uploaded", "extracted_fields": {"address": "wrong"}},
            {"employee_email": "fixture@example.test", "status": "deleted", "extracted_fields": {"address": "wrong"}}]
    assert not svc._employee_onboarding_profile(rows, employee_email="fixture@example.test",
                                               employee_request_id="join-1").get("address")


def test_preview_preserves_reader_gate_and_disposition(tmp_path, monkeypatch):
    import asyncio

    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("JWT_SECRET_KEY", "fixture-only-secret-key-at-least-32-characters")
    from app.api import obys_finance as api

    async def inline_reader(fn, *args):
        return fn(*args)

    monkeypatch.setattr(api, "run_in_threadpool", inline_reader)

    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF-1.4\nfixture")
    user = {"email": "fixture@example.test"}
    calls = []

    def reader(document_id, current_user):
        calls.append((document_id, current_user))
        return {"content_type": "application/pdf", "original_filename": "fixture.pdf"}, path

    monkeypatch.setattr(svc, "get_onboarding_document", reader)
    response = asyncio.run(api.preview_onboarding_document("fixture", user))
    assert calls == [("fixture", user)]
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["cache-control"] == "private, no-store"

    def unsupported(*args):
        return {"content_type": "image/svg+xml"}, path

    monkeypatch.setattr(svc, "get_onboarding_document", unsupported)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.preview_onboarding_document("fixture", user))
    assert exc.value.status_code == 415

    def denied(*args):
        raise HTTPException(status_code=404)

    monkeypatch.setattr(svc, "get_onboarding_document", denied)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.preview_onboarding_document("fixture", user))
    assert exc.value.status_code == 404


def test_camel_case_manual_bank_fields_win_over_extraction(monkeypatch):
    employee = {"id": "join-1", "status": "approved", "business_id": "biz-mia"}
    monkeypatch.setattr(svc, "_read_hr", lambda ledger, user: [employee] if ledger == "employee_join_requests" else [])
    monkeypatch.setattr(svc, "_contract_business", lambda *args: ("biz-mia", ""))
    monkeypatch.setattr(svc, "_employee_onboarding_profile", lambda *args, **kwargs: {
        "bank_name": "extracted bank", "bank_account_holder": "extracted holder"})
    monkeypatch.setattr(svc, "get_settings", lambda user: {})
    result = svc._fill_contract_reference_data({"employeeRequestId": "join-1", "bankName": "manual bank",
                                               "bankAccountHolder": "manual holder"}, {})
    assert result["bankName"] == "manual bank"
    assert result["bankAccountHolder"] == "manual holder"
    assert "bank_name" not in result
    assert "bank_account_holder" not in result


def test_upload_selects_exact_employment_and_rejects_ambiguous_request(tmp_path, monkeypatch):
    import asyncio
    from io import BytesIO

    import pytest
    from fastapi import HTTPException, UploadFile
    from starlette.datastructures import Headers

    rows = [
        {"id": "join-1", "email": "fixture@example.test", "status": "approved", "business_id": "biz-mia", "branch": "A"},
        {"id": "join-2", "email": "fixture@example.test", "status": "approved", "business_id": "biz-mia", "branch": "B"},
    ]
    written = []
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_require_owner_path_admin", lambda *args: None)
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_require_onboarding_business_scope", lambda *args: None)
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    monkeypatch.setattr(svc, "_read_hr", lambda *args: rows)
    monkeypatch.setattr(svc, "_read_file_rows", lambda *args: [])
    monkeypatch.setattr(svc, "_write_file_rows", lambda ledger, records: written.extend(records))
    monkeypatch.setattr(svc, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(svc, "_onboarding_document_view", lambda record: record)

    def upload(**extra):
        return asyncio.run(svc.save_onboarding_document(
            employee_name="fixture", employee_email="fixture@example.test", branch="", document_type="id_card",
            issue_date="", memo="", user={},
            upload=UploadFile(BytesIO(b"%PDF-1.4\nfixture"), filename="fixture.pdf",
                              headers=Headers({"content-type": "application/pdf"})), **extra))

    with pytest.raises(HTTPException) as exc:
        upload()
    assert exc.value.status_code == 409
    assert not list(tmp_path.iterdir())
    result = upload(employee_request_id="join-2", business_id="biz-mia")
    assert result["employee_request_id"] == "join-2"
    assert result["branch"] == "B"
    assert written[0]["employee_request_id"] == "join-2"
    with pytest.raises(HTTPException) as exc:
        upload(employee_request_id="foreign", business_id="biz-mia")
    assert exc.value.status_code == 404


def test_supersede_preserves_other_employment_documents():
    record = {"id": "new", "tenant_id": "fixture", "business_id": "biz-mia", "employee_email": "fixture@example.test",
              "employee_request_id": "join-2", "document_type": "id_card", "updated_at": "fixture-time"}
    rows = [{**record, "id": "other", "employee_request_id": "join-1", "status": "uploaded"},
            {**record, "id": "old", "status": "uploaded"}]
    assert svc._supersede_onboarding_file_rows(rows, record) == ["old"]
    assert rows[0]["status"] == "uploaded"

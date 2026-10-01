import importlib.util
import json
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

_PATH = Path(__file__).resolve().parents[2] / "app/services/yeoljeong_finance_service.py"
_SPEC = importlib.util.spec_from_file_location("o2_service", _PATH)
service = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(service)

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"


def user(membership=...):
    return {
        "email": "employee@example.com",
        "tenant_id": TENANT,
        "current_membership": {"tenant_id": TENANT, "status": "active", "role": "member"}
        if membership is ... else membership,
    }


@pytest.mark.parametrize("membership", [None, {}, {"tenant_id": TENANT, "status": "inactive"}, {"tenant_id": "other", "status": "active"}])
def test_membership_is_fail_closed(membership):
    with pytest.raises(HTTPException) as error:
        service._tenant_id(user(membership))
    assert error.value.status_code == 403


def test_payroll_physical_columns_override_forged_payload():
    row = {
        "id": "p1", "tenant_id": TENANT, "business_id": "biz-owned", "payroll_month": "2026-09",
        "gross_pay": "1000", "tax_withholding": 10, "insurance_deduction": 20,
        "other_deduction": 30, "net_pay": 940, "status": "confirmed",
        "statement_payload": json.dumps({"tenant_id": "other", "business_id": "biz-other", "gross_pay": 999999,
                                          "payroll_month": "1999-01", "status": "draft"}),
    }
    record = service._db_row_to_record("payroll_statements", row)
    assert (record["tenant_id"], record["business_id"], record["gross_pay"]) == (TENANT, "biz-owned", 1000)
    assert (record["payroll_month"], record["status"]) == ("2026-09", "confirmed")


def test_malformed_physical_payroll_amount_isolated_to_record():
    row = {
        "id": "bad", "tenant_id": TENANT, "business_id": "biz-owned", "payroll_month": "2026-09",
        "gross_pay": "1.5", "tax_withholding": 0, "insurance_deduction": 0,
        "other_deduction": 0, "net_pay": 0, "status": "draft", "statement_payload": {},
    }
    record = service._db_row_to_record("payroll_statements", row)
    assert record["gross_pay"] is None
    assert record["payroll_validation_errors"] == ["gross_pay"]


@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, "1.0", "1e3", "NaN", object(), 9_000_000_000_000_001])
def test_payroll_integer_rejects_ambiguous_or_out_of_range_values(value):
    with pytest.raises(ValueError):
        service._payroll_integer(value, field="gross_pay")


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (42, 42), ("42", 42), ("-42", -42), ("+42", 42), (None, 0), ("", 0)])
def test_payroll_integer_preserves_integer_compatibility(value, expected):
    assert service._payroll_integer(value, field="gross_pay") == expected


def test_hr_file_read_hides_cross_tenant_and_unassigned(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(service, "_db_available", lambda: False)
    service._write_file_rows("contracts", [
        {"id": "owned", "tenant_id": TENANT}, {"id": "cross", "tenant_id": "other"}, {"id": "legacy"},
    ])
    assert [row["id"] for row in service._read_hr("contracts", user())] == ["owned"]


def test_cross_tenant_identifier_is_not_disclosed():
    with pytest.raises(HTTPException) as error:
        service._require_hr_record({"id": "secret", "tenant_id": "other"}, user(), detail="not found")
    assert error.value.status_code == 404


def test_hr_upsert_sql_prevents_tenant_takeover():
    source = _PATH.read_text(encoding="utf-8")
    for table in ("yeoljeong_employee_join_requests", "yeoljeong_onboarding_documents", "yeoljeong_contracts", "yeoljeong_payroll_statements"):
        assert f"WHERE {table}.tenant_id = EXCLUDED.tenant_id" in source


# --- O2-3 직원 서류등록 백엔드 -------------------------------------------------
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n"
OWNER = {
    "email": "owner@example.com", "tenant_id": TENANT,
    "current_membership": {"tenant_id": TENANT, "status": "active", "role": "owner"},
}
ADMIN_MIA = {
    "email": "admin-mia@example.com", "tenant_id": TENANT,
    "current_membership": {"tenant_id": TENANT, "status": "active", "role": "member"},
}
EMP_MIA = "emp-mia@example.com"


@pytest.fixture
def hr(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(service, "_db_available", lambda: False)
    base = {"tenant_id": TENANT, "status": "approved"}
    service._write_file_rows("employee_join_requests", [
        {"id": "join-emp-mia", "name": "미아직원", "email": EMP_MIA, "business_id": "biz-mia", "branch": "열정국밥_미아점", **base},
        {"id": "join-emp-jh", "name": "중화직원", "email": "emp-jh@example.com", "business_id": "biz-junghwa", "branch": "중화점", **base},
        {"id": "join-admin-mia", "name": "미아관리자", "email": ADMIN_MIA["email"], "business_id": "biz-mia",
         "branch": "열정국밥_미아점", "role": "admin", **base},
    ])
    return tmp_path


def _upload(name, data, email=EMP_MIA, document_type="bankbook", **kwargs):
    return service.save_onboarding_document(
        employee_name="직원", employee_email=email, branch="", document_type=document_type,
        issue_date=kwargs.pop("issue_date", ""), memo="", user=kwargs.pop("user", OWNER),
        upload=UploadFile(filename=name, file=BytesIO(data)), **kwargs,
    )


def _file_rows():
    return service._read_file_rows("onboarding_documents")


def _stored_files(root):
    return [p for p in (root / "uploads" / "onboarding").glob("*") if p.is_file()]


def test_onboarding_extensions_match_business_documents():
    assert service.ONBOARDING_DOCUMENT_EXTENSIONS == service.BUSINESS_DOCUMENT_EXTENSIONS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["virus.exe", "run.sh", "page.html", "noext"])
async def test_disallowed_extension_is_rejected_400(hr, name):
    with pytest.raises(HTTPException) as error:
        await _upload(name, PDF)
    assert error.value.status_code == 400
    assert _file_rows() == [] and _stored_files(hr) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "data"), [
    ("fake.pdf", b"<html><script>alert(1)</script></html>"),
    ("fake.png", PDF),
    ("fake.jpg", b"\x89PNG\r\n"),
    ("fake.webp", b"RIFF\x00\x00\x00\x00WAVE"),
])
async def test_pdf_or_image_with_wrong_magic_bytes_is_rejected_and_removed(hr, name, data):
    with pytest.raises(HTTPException) as error:
        await _upload(name, data)
    assert error.value.status_code == 400
    assert _file_rows() == []
    assert list((hr / "uploads" / "onboarding").glob("*")) == []


@pytest.mark.asyncio
async def test_empty_file_is_rejected(hr):
    with pytest.raises(HTTPException) as error:
        await _upload("empty.pdf", b"")
    assert error.value.status_code == 400
    assert list((hr / "uploads" / "onboarding").glob("*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "data"), [
    ("a.pdf", PDF), ("a.jpg", b"\xff\xd8\xff\xe0JFIF"), ("a.png", b"\x89PNG\r\n\x1a\n"),
    ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "), ("a.docx", b"PK\x03\x04"), ("a.hwp", b"HWP Document File"),
])
async def test_valid_signatures_are_accepted_with_integrity_fields(hr, name, data):
    saved = await _upload(name, data)
    assert saved["status"] == "uploaded" and "stored_path" not in saved
    row = _file_rows()[0]
    assert len(row["sha256"]) == 64 and row["stored_path"] == row["stored_filename"]
    path = hr / "uploads" / "onboarding" / row["stored_path"]
    assert path.read_bytes() == data and (path.stat().st_mode & 0o777) == 0o600
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("document_type", sorted(service.ONBOARDING_EXPIRY_REQUIRED_TYPES))
async def test_expiry_required_types_reject_missing_expires_at(hr, document_type):
    with pytest.raises(HTTPException) as error:
        await _upload("a.pdf", PDF, document_type=document_type)
    assert error.value.status_code == 400 and "만료일" in error.value.detail
    assert _file_rows() == [] and _stored_files(hr) == []
    saved = await _upload("a.pdf", PDF, document_type=document_type, expires_at="2027-01-31")
    assert saved["expires_at"] == "2027-01-31"


@pytest.mark.asyncio
async def test_expiry_before_issue_date_is_rejected(hr):
    with pytest.raises(HTTPException) as error:
        await _upload("a.pdf", PDF, document_type="health_certificate", issue_date="2026-09-01", expires_at="2026-08-01")
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        await _upload("a.pdf", PDF, expires_at="내일")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_expiry_flags_in_list_29_days_left_is_expiring(hr):
    today = service.datetime.now(service.KST).date()
    await _upload("a.pdf", PDF, document_type="health_certificate", expires_at=(today + timedelta(days=29)).isoformat())
    await _upload("b.pdf", PDF, document_type="foreign_registration", expires_at=(today + timedelta(days=31)).isoformat())
    await _upload("c.pdf", PDF, document_type="visa_status_certificate", expires_at=(today - timedelta(days=1)).isoformat())
    await _upload("d.pdf", PDF, document_type="id_card")
    rows = {r["document_type"]: r for r in service.list_onboarding_documents(OWNER) if not r.get("is_placeholder")}
    soon, later, past, none = (rows[k] for k in ("health_certificate", "foreign_registration", "visa_status_certificate", "id_card"))
    assert (soon["is_expiring"], soon["is_expired"], soon["days_until_expiry"], soon["expiry_status"]) == (True, False, 29, "expiring")
    assert (later["is_expiring"], later["is_expired"], later["expiry_status"]) == (False, False, "")
    assert (past["is_expired"], past["is_expiring"], past["expiry_label"]) == (True, False, "만료")
    assert (none["is_expiring"], none["is_expired"], none["days_until_expiry"], none["expires_at"]) == (False, False, None, "")
    assert all("stored_path" not in r for r in rows.values())


@pytest.mark.asyncio
async def test_reupload_marks_previous_superseded_and_keeps_all_rows(hr):
    first = await _upload("a.pdf", PDF)
    review = service.review_onboarding_document(first["id"], "approved", "ok", OWNER)
    assert review["status"] == "approved"
    other_type = await _upload("o.pdf", PDF, document_type="id_card")
    other_emp = await _upload("e.pdf", PDF, email="emp-jh@example.com")
    second = await _upload("b.pdf", PDF)
    third = await _upload("c.pdf", PDF)
    rows = {r["id"]: r for r in _file_rows()}
    assert len(rows) == 5
    assert rows[first["id"]]["status"] == "superseded" and rows[first["id"]]["superseded_by"] == second["id"]
    assert rows[second["id"]]["status"] == "superseded" and rows[second["id"]]["superseded_by"] == third["id"]
    assert rows[third["id"]]["status"] == "uploaded" and rows[third["id"]]["superseded_by"] == ""
    assert rows[other_type["id"]]["status"] == "uploaded" and rows[other_emp["id"]]["status"] == "uploaded"
    assert len(_stored_files(hr)) == 5
    listed = [r for r in service.list_onboarding_documents(OWNER) if not r.get("is_placeholder")]
    assert len(listed) == 5
    with pytest.raises(HTTPException) as error:
        service.review_onboarding_document(first["id"], "approved", "", OWNER)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_rejected_document_yields_resubmit_required_placeholder(hr):
    mia_missing = lambda: {  # noqa: E731
        r["document_type"]: r for r in service.list_onboarding_documents(OWNER)
        if r.get("is_placeholder") and r["employee_email"] == EMP_MIA
    }
    initial = mia_missing()
    assert {r["status"] for r in initial.values()} == {"missing"}
    bank = await _upload("a.pdf", PDF)
    assert "bankbook" not in mia_missing()
    service.review_onboarding_document(bank["id"], "rejected", "흐림", OWNER)
    placeholders = mia_missing()
    assert placeholders["bankbook"]["status"] == "resubmit_required"
    assert placeholders["bankbook"]["status_label"] == "재제출 필요"
    assert placeholders["bankbook"]["previous_document_id"] == bank["id"]
    assert placeholders["bankbook"]["review_memo"] == "흐림"
    assert placeholders["id_card"]["status"] == "missing"
    service.review_onboarding_document(bank["id"], "needs_fix", "보완", OWNER)
    assert mia_missing()["bankbook"]["status"] == "resubmit_required"
    again = await _upload("b.pdf", PDF)
    assert "bankbook" not in mia_missing()
    service.review_onboarding_document(again["id"], "approved", "", OWNER)
    assert "bankbook" not in mia_missing()


@pytest.mark.asyncio
async def test_other_business_document_is_403_for_download_review_delete(hr):
    jh = await _upload("a.pdf", PDF, email="emp-jh@example.com")
    mia = await _upload("m.pdf", PDF)
    for call in (
        lambda: service.get_onboarding_document(jh["id"], ADMIN_MIA),
        lambda: service.review_onboarding_document(jh["id"], "approved", "", ADMIN_MIA),
        lambda: service.delete_onboarding_document(jh["id"], ADMIN_MIA),
    ):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 403
    assert {r["id"]: r["status"] for r in _file_rows()}[jh["id"]] == "uploaded"
    assert not any(r.get("deleted_at") for r in _file_rows())
    record, path = service.get_onboarding_document(mia["id"], ADMIN_MIA)
    assert record["id"] == mia["id"] and path.is_file()
    assert service.get_onboarding_document(jh["id"], OWNER)[1].is_file()
    assert service.review_onboarding_document(mia["id"], "approved", "", ADMIN_MIA)["status"] == "approved"


@pytest.mark.asyncio
async def test_download_blocks_tampered_file_with_409(hr):
    saved = await _upload("a.pdf", PDF)
    _, path = service.get_onboarding_document(saved["id"], OWNER)
    path.write_bytes(PDF + b"tampered")
    with pytest.raises(HTTPException) as error:
        service.get_onboarding_document(saved["id"], OWNER)
    assert error.value.status_code == 409


def test_legacy_row_without_hash_still_downloads_by_stored_filename(hr):
    stored = hr / "uploads" / "onboarding"
    stored.mkdir(parents=True, exist_ok=True)
    (stored / "legacy.pdf").write_bytes(b"old")
    service._write_file_rows("onboarding_documents", [{
        "id": "legacy", "tenant_id": TENANT, "employee_email": EMP_MIA, "business_id": "biz-mia",
        "stored_filename": "legacy.pdf", "status": "approved",
    }])
    assert service.get_onboarding_document("legacy", OWNER)[1].read_bytes() == b"old"
    service._write_file_rows("onboarding_documents", [{
        "id": "evil", "tenant_id": TENANT, "employee_email": EMP_MIA, "business_id": "biz-mia",
        "stored_filename": "../../../etc/passwd", "status": "approved",
    }])
    with pytest.raises(HTTPException) as error:
        service.get_onboarding_document("evil", OWNER)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_file_mode_delete_keeps_row_and_original_file(hr):
    saved = await _upload("a.pdf", PDF)
    service.delete_onboarding_document(saved["id"], OWNER)
    rows = _file_rows()
    assert [r["id"] for r in rows] == [saved["id"]] and rows[0]["deleted_at"]
    assert len(_stored_files(hr)) == 1
    assert all(r["id"] != saved["id"] for r in service.list_onboarding_documents(OWNER))
    with pytest.raises(HTTPException) as error:
        service.delete_onboarding_document(saved["id"], OWNER)
    assert error.value.status_code == 404


def test_migration_adds_only_the_four_columns_and_rollback_reverses():
    root = Path(__file__).resolve().parents[2]
    up = (root / "migrations/20261001_obys_hrdoc_expiry_integrity_superseded.sql").read_text(encoding="utf-8")
    down = (root / "migrations/rollback/20261001_obys_hrdoc_expiry_integrity_superseded.down.sql").read_text(encoding="utf-8")
    for column, ddl in (
        ("expires_at", "expires_at DATE NULL"), ("sha256", "sha256 TEXT NOT NULL DEFAULT ''"),
        ("stored_path", "stored_path TEXT NOT NULL DEFAULT ''"), ("superseded_by", "superseded_by TEXT NOT NULL DEFAULT ''"),
    ):
        assert f"ADD COLUMN IF NOT EXISTS {ddl}" in up
        assert f"DROP COLUMN IF EXISTS {column};" in down
    for statement in ("UPDATE ", "DELETE ", "INSERT ", "DROP TABLE"):
        assert statement not in up.replace("-- ", "")
    assert "migrations/20261001_obys_hrdoc_expiry_integrity_superseded.sql" in (
        root / "scripts/migrations_auto_apply_baseline.txt"
    ).read_text(encoding="utf-8")


def test_hr_docs_list_sql_keeps_old_columns_and_adds_new_ones():
    source = (Path(__file__).resolve().parents[2] / "app/api/obys_workspaces.py").read_text(encoding="utf-8")
    line = next(row for row in source.splitlines() if row.strip().startswith('"hr-docs"'))
    for column in ("id", "employee_name", "branch", "document_type", "document_label", "status",
                   "original_filename", "issue_date", "uploaded_at", "expires_at", "superseded_by"):
        assert f"{column}," in line.replace("FROM", ",FROM") or f",{column} " in line

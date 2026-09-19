import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

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

"""AADS-OBYS-MULTISTORE-EMPLOYMENT-KEY-20261001 — 한 직원이 서로 다른 사업자(매장) 여럿에서 일하는 겸직.

3개 매장은 같은 사업자의 branch 가 아니라 별개 business(별개 고용주)다. 가입요청·고용조건·
미비 집계·출퇴근 유니크 키가 모두 (직원, business_id) 단위로 갈려야 한다.
파일 모드로만 돈다(운영 DB 미접촉).
"""
import os
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api

svc = api.svc
ROOT = Path(__file__).resolve().parents[2]
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMP_EMAIL = "parttimer@example.com"
EMPLOYEE = {"email": EMP_EMAIL, "user_id": "user-pt", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}

MIA = {"business_id": "biz-mia", "branch": "열정국밥_미아점"}
JUNGHWA = {"business_id": "biz-junghwa", "branch": "중화점"}
SUNGSHIN = {"business_id": "biz-sungshin", "branch": "성신여대점"}


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    return tmp_path


def _request(store: dict, **extra) -> dict:
    return svc.upsert_join_request({"name": "겸직 직원", "email": EMP_EMAIL, **store, **extra}, EMPLOYEE)


def _rows() -> list[dict]:
    return svc._read_hr("employee_join_requests", ADMIN)


def _contract(contract_id: str, store: dict, wage: int, signed_at: str, *, request_id: str = "") -> dict:
    return {
        "id": contract_id, "tenant_id": TENANT, "employee_email": EMP_EMAIL,
        "employee_request_id": request_id, "contract_type": "part_time", "status": "signed",
        "signed_at": signed_at, "wage": str(wage), "business_id": store["business_id"], "branch": store["branch"],
    }


# --- 1. A: (email, business_id) 로 식별 -------------------------------------------
def test_second_store_request_keeps_first_store_request():
    first = _request(MIA)
    second = _request(JUNGHWA)
    rows = _rows()
    assert len(rows) == 2
    assert first["id"] != second["id"]
    assert {row["business_id"] for row in rows} == {"biz-mia", "biz-junghwa"}
    # 같은 매장 재요청은 갱신이지 신규가 아니다.
    again = _request(MIA, memo="다시")
    assert again["id"] == first["id"]
    assert len(_rows()) == 2


def test_three_stores_three_requests():
    for store in (MIA, JUNGHWA, SUNGSHIN):
        _request(store)
    assert len(_rows()) == 3


def test_branch_only_payload_resolves_business_and_does_not_overwrite_other_store():
    first = _request(MIA)
    second = svc.upsert_join_request({"name": "직원", "email": EMP_EMAIL, "branch": "중화점"}, EMPLOYEE)
    assert second["id"] != first["id"]
    assert second["business_id"] == "biz-junghwa"
    assert len(_rows()) == 2


def test_payload_without_branch_or_business_keeps_existing_values():
    first = _request(MIA)
    updated = svc.upsert_join_request({"name": "이름변경", "email": EMP_EMAIL}, EMPLOYEE)
    assert updated["id"] == first["id"]
    assert updated["business_id"] == "biz-mia"
    assert updated["branch"] == "열정국밥_미아점"
    assert updated["name"] == "이름변경"
    assert len(_rows()) == 1


# --- 2. 하위호환: business_id 빈 기존 행 1건은 채워 갱신 -------------------------------
def test_single_blank_business_row_is_filled_not_duplicated():
    svc._write("employee_join_requests", [
        {"id": "legacy-1", "name": "기존", "email": EMP_EMAIL, "tenant_id": TENANT, "business_id": "",
         "branch": "", "status": "pending", "requested_at": "2026-09-01T09:00:00+09:00"},
    ])
    record = _request(JUNGHWA)
    rows = _rows()
    assert len(rows) == 1, "유령 요청이 남으면 안 된다"
    assert record["id"] == "legacy-1"
    assert rows[0]["business_id"] == "biz-junghwa"
    assert rows[0]["branch"] == "중화점"


def test_blank_business_row_does_not_swallow_a_request_when_matching_business_exists():
    svc._write("employee_join_requests", [
        {"id": "legacy-1", "email": EMP_EMAIL, "tenant_id": TENANT, "business_id": "", "branch": "", "status": "pending"},
        {"id": "mia-1", "email": EMP_EMAIL, "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점",
         "status": "pending"},
    ])
    record = _request(MIA)
    assert record["id"] == "mia-1"
    assert len(_rows()) == 2


def test_payload_without_business_on_blank_row_keeps_prior_behavior():
    # 사업자를 특정하지 않으면 종전처럼 이메일로 그 행을 잡고, 사업자 없는 기록은 종전 가드(400)에 걸린다.
    svc._write("employee_join_requests", [
        {"id": "legacy-1", "email": EMP_EMAIL, "tenant_id": TENANT, "business_id": "", "branch": "", "status": "pending"},
    ])
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": "이름", "email": EMP_EMAIL}, EMPLOYEE)
    assert excinfo.value.status_code == 400
    assert [row["id"] for row in _rows()] == ["legacy-1"]


# --- 3. 빈 business_id 행 2건 이상이면 400 -------------------------------------------
def test_two_blank_business_rows_reject_with_400():
    svc._write("employee_join_requests", [
        {"id": "legacy-1", "email": EMP_EMAIL, "tenant_id": TENANT, "business_id": "", "branch": "", "status": "pending"},
        {"id": "legacy-2", "email": EMP_EMAIL, "tenant_id": TENANT, "business_id": "", "branch": "", "status": "pending"},
    ])
    with pytest.raises(HTTPException) as excinfo:
        _request(JUNGHWA)
    assert excinfo.value.status_code == 400
    assert "수동 정리" in str(excinfo.value.detail)
    assert len(_rows()) == 2


# --- 6. branch·business 불일치 가드 보존 ----------------------------------------------
def test_branch_business_mismatch_still_400():
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(
            {"name": "직원", "email": EMP_EMAIL, "business_id": "biz-junghwa", "branch": "열정국밥_미아점"}, EMPLOYEE
        )
    assert excinfo.value.status_code == 400
    assert "일치하지 않습니다" in str(excinfo.value.detail)
    assert _rows() == []


def test_mismatch_against_existing_row_is_still_400():
    _request(MIA)
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(
            {"name": "직원", "email": EMP_EMAIL, "business_id": "biz-mia", "branch": "중화점"}, EMPLOYEE
        )
    assert excinfo.value.status_code == 400


def test_requester_identity_is_still_pinned():
    record = _request(MIA)
    assert record["requester_user_id"] == "user-pt"
    assert record["requester_email"] == EMP_EMAIL


# --- 4. B: 매장별 고용조건 ------------------------------------------------------------
def _three_store_contracts() -> list[dict]:
    return [
        _contract("c-mia", MIA, 10320, "2026-09-01T10:00:00+09:00"),
        _contract("c-junghwa", JUNGHWA, 11000, "2026-09-02T10:00:00+09:00"),
        _contract("c-sungshin", SUNGSHIN, 12000, "2026-09-03T10:00:00+09:00"),
    ]


def test_derive_current_employment_is_per_business():
    contracts = _three_store_contracts()
    wages = {}
    for store in (MIA, JUNGHWA, SUNGSHIN):
        derived = svc._derive_current_employment(
            contracts, employee_email=EMP_EMAIL, employee_request_id="", business_id=store["business_id"]
        )
        wages[store["business_id"]] = derived["employment"]["wage"]
        assert derived["contract_id"] == {"biz-mia": "c-mia", "biz-junghwa": "c-junghwa", "biz-sungshin": "c-sungshin"}[
            store["business_id"]
        ]
    assert wages == {"biz-mia": 10320, "biz-junghwa": 11000, "biz-sungshin": 12000}


def test_derive_current_employment_without_business_keeps_latest_overall():
    derived = svc._derive_current_employment(_three_store_contracts(), employee_email=EMP_EMAIL, employee_request_id="")
    assert derived["contract_id"] == "c-sungshin"


def test_derive_current_employment_unknown_business_is_none():
    contracts = [c for c in _three_store_contracts() if c["id"] != "c-junghwa"]
    assert svc._derive_current_employment(
        contracts, employee_email=EMP_EMAIL, employee_request_id="", business_id="biz-junghwa"
    ) is None


def test_signed_contracts_filter_by_business():
    rows = svc._employee_signed_contracts(
        _three_store_contracts(), employee_email=EMP_EMAIL, employee_request_id="", business_id="biz-mia"
    )
    assert [row["id"] for row in rows] == ["c-mia"]
    assert len(svc._employee_signed_contracts(
        _three_store_contracts(), employee_email=EMP_EMAIL, employee_request_id=""
    )) == 3


def test_snapshot_is_current_is_judged_per_store_employee_row():
    contracts = _three_store_contracts()
    derived = svc._derive_current_employment(
        contracts, employee_email=EMP_EMAIL, employee_request_id="", business_id="biz-mia"
    )
    mia_employee = {"current_employment_contract_id": "c-mia", "current_employment": derived["employment"]}
    assert svc._employment_snapshot_is_current(mia_employee, derived)
    other = svc._derive_current_employment(
        contracts, employee_email=EMP_EMAIL, employee_request_id="", business_id="biz-junghwa"
    )
    assert not svc._employment_snapshot_is_current(mia_employee, other)


# --- 5. C: 매장별 미비 판정 -----------------------------------------------------------
def _approved_two_store_employee():
    rows = [
        {"id": "join-mia", "name": "겸직", "email": EMP_EMAIL, "tenant_id": TENANT, "status": "approved",
         "requested_at": "2026-09-01T09:00:00+09:00", **MIA},
        {"id": "join-junghwa", "name": "겸직", "email": EMP_EMAIL, "tenant_id": TENANT, "status": "approved",
         "requested_at": "2026-09-02T09:00:00+09:00", **JUNGHWA},
    ]
    svc._write("employee_join_requests", rows)
    svc._write("contracts", [_contract("c-mia", MIA, 10320, "2026-09-01T10:00:00+09:00", request_id="join-mia")])
    svc._write("payroll_statements", [
        {"id": "p-mia", "tenant_id": TENANT, "employee_email": EMP_EMAIL, "business_id": "biz-mia", "branch": "열정국밥_미아점"},
    ])
    svc._write("onboarding_documents", [
        {"id": "d-mia", "tenant_id": TENANT, "employee_email": EMP_EMAIL, "business_id": "biz-mia",
         "branch": "열정국밥_미아점", "document_type": "id_card", "status": "확인"},
    ])


def test_list_approved_employees_counts_are_per_business():
    _approved_two_store_employee()
    junghwa = svc.list_approved_employees(ADMIN, business_id="biz-junghwa")
    assert [row["id"] for row in junghwa] == ["join-junghwa"]
    assert junghwa[0]["contract_count"] == 0
    assert junghwa[0]["payroll_statement_count"] == 0
    assert junghwa[0]["onboarding_document_count"] == 0
    assert junghwa[0]["needs_contract"] and junghwa[0]["needs_payroll"] and junghwa[0]["needs_onboarding_documents"]

    mia = svc.list_approved_employees(ADMIN, business_id="biz-mia")
    assert [row["id"] for row in mia] == ["join-mia"]
    assert mia[0]["contract_count"] == 1
    assert mia[0]["payroll_statement_count"] == 1
    assert mia[0]["onboarding_document_count"] == 1
    assert not (mia[0]["needs_contract"] or mia[0]["needs_payroll"] or mia[0]["needs_onboarding_documents"])


def test_list_approved_employees_unscoped_returns_one_row_per_store():
    _approved_two_store_employee()
    everyone = svc.list_approved_employees(ADMIN)
    assert sorted(row["id"] for row in everyone) == ["join-junghwa", "join-mia"]
    by_id = {row["id"]: row for row in everyone}
    assert by_id["join-mia"]["contract_count"] == 1
    assert by_id["join-junghwa"]["contract_count"] == 0


def test_list_approved_employees_passes_business_to_employment_derivation():
    _approved_two_store_employee()
    svc._write("contracts", [
        _contract("c-mia", MIA, 10320, "2026-09-01T10:00:00+09:00", request_id="join-mia"),
        _contract("c-junghwa", JUNGHWA, 11000, "2026-09-02T10:00:00+09:00", request_id="join-junghwa"),
    ])
    rows = {row["id"]: row for row in svc.list_approved_employees(ADMIN)}
    # 스냅샷이 없으니 둘 다 동기화 필요 — 각자 자기 매장 계약 기준이어야 한다.
    assert rows["join-mia"]["needs_employment_sync"] is True
    assert rows["join-junghwa"]["needs_employment_sync"] is True
    derived = svc._derive_current_employment(
        svc._read_hr("contracts", ADMIN), employee_email=EMP_EMAIL, employee_request_id="join-junghwa",
        business_id="biz-junghwa",
    )
    rows["join-junghwa"].update(current_employment=derived["employment"], current_employment_contract_id="c-junghwa")
    assert svc._employment_snapshot_is_current(rows["join-junghwa"], derived)


# --- D·E: 출퇴근 유니크 키 (마이그레이션·ON CONFLICT 정합) ---------------------------------
def test_attendance_migration_files_define_business_scoped_keys():
    up = (ROOT / "migrations/20261001_obys_attendance_business_scoped_unique.sql").read_text(encoding="utf-8")
    down = (ROOT / "migrations/rollback/20261001_obys_attendance_business_scoped_unique.down.sql").read_text(encoding="utf-8")
    assert "UNIQUE (employee_email, business_id, week_start)" in up
    assert "UNIQUE (employee_email, business_id, work_date, start_at)" in up
    assert "HAVING count(*) > 1" in up and "RAISE EXCEPTION" in up
    assert "UNIQUE (employee_email, week_start)" in down
    assert "UNIQUE (employee_email, work_date, start_at)" in down
    baseline = (ROOT / "scripts/migrations_auto_apply_baseline.txt").read_text(encoding="utf-8")
    assert "migrations/20261001_obys_attendance_business_scoped_unique.sql" in baseline


def test_attendance_on_conflict_matches_new_unique_key():
    source = (ROOT / "app/api/obys_workspaces.py").read_text(encoding="utf-8")
    targets = re.findall(r"ON CONFLICT \(([^)]*)\)", source)
    assert "employee_email,business_id,work_date,start_at" in targets
    assert "employee_email,work_date,start_at" not in targets

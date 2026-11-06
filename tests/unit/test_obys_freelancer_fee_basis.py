"""ACCT-OBYS-FREELANCER-FEE-BASIS-20261106 — 프리랜서 용역비 산정 기준(건별·시급·일급·월정액).

구조: 프리랜서는 wage_type=case_fee 고정 + fee_basis + fee_rate. 옛 구조(wage_type=hourly)는 시급으로 읽는다.
"""
import base64
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, obys_contract_clauses, yeoljeong_ops_service
from app.services import yeoljeong_contract_pdf as pdf

svc = api.svc
REPO = Path(__file__).resolve().parents[2]
MODULES = REPO / "app" / "static" / "apps" / "obys" / "modules"
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
LABOR_NOTICE = "정해진 요일·시간에 지시를 받고 일하면 근로자로 판정될 수 있습니다 — 노무 확인 권장"


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    clock = {"now": datetime(2026, 11, 6, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf-test", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"],
        "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
        "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])
    return tmp_path


BASE = {
    "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
    "contract_date": "2026-11-06", "start_date": "2026-11-01", "workplace": "열정국밥 미아점",
}


def freelancer(fee_basis="lump", rate=500000, **overrides):
    payload = {
        **BASE, "contract_type": "freelancer", "employment_tax_type": "freelancer_33", "wage_type": "case_fee",
        "fee_basis": fee_basis, "fee_rate": rate, "wage": rate,
        "freelancer_scope": "매장 운영 지원 용역", "freelancer_settlement_terms": "합의된 방식으로 정산",
    }
    payload.update(overrides)
    return payload


def legacy_hourly(rate=13000, **overrides):
    payload = freelancer(rate=rate, wage_type="hourly", **overrides)
    payload.pop("fee_basis")
    payload.pop("fee_rate")
    return payload


def _signature_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def sign(payload):
    saved = svc.save_contract(payload, ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    return svc.sign_contract_and_deliver(
        {
            "token": requested["sign_token"], "signer_name": "가입 직원", "consent": True,
            "consent_version": "yeoljeong-contract-sign-v1",
            "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii"),
        },
        EMPLOYEE,
    )


def make_legacy(contract_id):
    """서명 완료 원본을 fee_basis 도입 전 구조(wage_type=hourly, fee 필드 없음)로 되돌린 상태를 만든다."""
    rows = svc._read_hr("contracts", ADMIN)
    for row in rows:
        if row["id"] == contract_id:
            for target in (row, row.get("signed_snapshot") or {}):
                target["wage_type"] = "hourly"
                target.pop("fee_basis", None)
                target.pop("fee_rate", None)
    svc._write("contracts", rows)
    people = svc._read_hr("employee_join_requests", ADMIN)
    for person in people:
        snapshot = person.get("current_employment")
        if snapshot:
            snapshot["wage_type"] = "hourly"
            snapshot.pop("fee_basis", None)
            snapshot.pop("fee_rate", None)
    svc._write("employee_join_requests", people)


def stored(contract_id):
    return next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract_id)


# --- 1. 저장: case_fee + 네 가지 fee_basis -------------------------------------


@pytest.mark.parametrize("basis,rate", [("lump", 500000), ("hourly", 12500), ("daily", 90000), ("monthly", 2000000)])
def test_save_stores_case_fee_with_fee_basis_and_rate(basis, rate):
    saved = svc.save_contract(freelancer(basis, rate), ADMIN)
    row = stored(saved["id"])
    assert row["wage_type"] == "case_fee"
    assert row["fee_basis"] == basis
    assert row["fee_rate"] == rate == row["wage"]


def test_fee_basis_defaults_to_lump_and_rejects_unknown():
    payload = freelancer()
    payload.pop("fee_basis")
    assert stored(svc.save_contract(payload, ADMIN)["id"])["fee_basis"] == "lump"
    with pytest.raises(HTTPException) as exc:
        svc.save_contract(freelancer("weekly"), ADMIN)
    assert exc.value.status_code == 400
    assert "건별·시급·일급·월정액" in str(exc.value.detail)


@pytest.mark.parametrize("basis,label", [("lump", "확정 용역비"), ("hourly", "확정 시급"), ("daily", "확정 일급"), ("monthly", "월 용역비")])
def test_missing_rate_message_follows_basis(basis, label):
    with pytest.raises(HTTPException) as exc:
        svc.save_contract(freelancer(basis, 0), ADMIN)
    assert label in str(exc.value.detail)


def test_fee_fields_are_not_stored_on_employment_contracts():
    saved = svc.save_contract({
        **BASE, "contract_type": "part_time", "employment_tax_type": "four_insurance", "wage_type": "hourly",
        "wage": 11000, "fee_basis": "daily", "fee_rate": 5, "weekly_hours": "주 20시간", "work_days": "월·수·금",
        "job_description": "매장", "work_time": "09:00-15:00", "rest_time": "30분", "holidays": "일요일",
        "pay_date": "매월 10일", "pay_method": "계좌이체", "wage_composition": "기본급", "overtime_terms": "승인",
        "leave_terms": "법정", "insurance_terms": "법정", "daily_work_schedule": "월수금 6시간",
        "meal_provision": "employer_meal",
    }, ADMIN)
    row = stored(saved["id"])
    assert row["wage_type"] == "hourly" and "fee_basis" not in row and "fee_rate" not in row


# --- 2. 옛 시급 프리랜서 호환 ---------------------------------------------------


def test_legacy_hourly_freelancer_is_read_as_hourly_and_signed_copy_is_unchanged():
    assert svc.freelancer_fee_basis("freelancer", "hourly", None) == "hourly"
    assert svc.freelancer_fee_basis("freelancer", "case_fee", None) == "lump"
    assert svc.freelancer_fee_basis("freelancer", "case_fee", "daily") == "daily"
    assert svc.freelancer_fee_basis("part_time", "hourly", "daily") == ""
    result = sign(freelancer("hourly", 13000))
    make_legacy(result["contract"]["id"])
    signed = stored(result["contract"]["id"])
    assert signed["status"] == "signed"
    assert svc.freelancer_fee_basis(signed["contract_type"], signed["wage_type"], signed.get("fee_basis")) == "hourly"
    assert signed["wage_type"] == "hourly" and "fee_basis" not in signed and signed["wage"] == 13000
    # 읽기·목록 조회가 서명 원본을 새 구조로 바꾸지 않는다.
    svc.list_contracts(ADMIN)
    svc.list_approved_employees(ADMIN)
    again = stored(signed["id"])
    assert again["wage_type"] == "hourly" and "fee_basis" not in again and again["wage"] == 13000


def test_unsigned_legacy_hourly_draft_is_normalized_only_on_resave():
    first = svc.save_contract(legacy_hourly(), ADMIN)
    assert stored(first["id"])["wage_type"] == "case_fee"
    assert stored(first["id"])["fee_basis"] == "hourly"
    assert stored(first["id"])["fee_rate"] == 13000


def test_legacy_hourly_clause_and_pdf_text_unchanged():
    legacy = {**legacy_hourly(), "freelancer_settlement_terms": "시급 13,000원 × 승인된 근태 시간"}
    text = "\n".join(f"{t} {b}" for t, b in obys_contract_clauses.contract_clauses(legacy))
    assert "3.3% 프리랜서 원천징수 · 시급" in text
    rows = dict(pdf.term_rows({"contract_type": "freelancer", "wage_type": "hourly", "wage": 13000,
                               "employment_tax_type": "freelancer_33"}))
    assert rows.get("시급") == 13000


# --- 3. 계약 문구 ---------------------------------------------------------------


@pytest.mark.parametrize("basis,rate,expected", [
    ("hourly", 12500, "용역비 산정: 시간당 12,500원 × 승인된 수행 시간"),
    ("daily", 90000, "1일 90,000원 × 수행일수"),
    ("monthly", 2000000, "월 2,000,000원(정액)"),
    ("lump", 500000, "건별 확정 500,000원"),
])
def test_contract_wording_per_basis(basis, rate, expected):
    text = "\n".join(f"{t} {b}" for t, b in obys_contract_clauses.contract_clauses(freelancer(basis, rate)))
    assert expected in text
    assert "기본급" not in text and "식대" not in text


def test_fixed_commute_block_is_kept_in_contract_text():
    for basis in ("lump", "hourly", "daily", "monthly"):
        text = "\n".join(f"{t} {b}" for t, b in obys_contract_clauses.contract_clauses(freelancer(basis, 12500)))
        assert "고정 출퇴근, 매장 상시 근무표, 교대조 편입으로 운영하지 않는다" in text


def test_pdf_term_rows_show_basis_and_rate():
    rows = dict(pdf.term_rows({"contract_type": "freelancer", "wage_type": "case_fee", "fee_basis": "daily",
                               "fee_rate": 90000, "wage": 90000, "employment_tax_type": "freelancer_33"}))
    assert rows.get("산정 기준") == "일급" or "일급" in rows.values()
    assert 90000 in rows.values()
    assert "fee_basis" not in rows and "fee_rate" not in rows


# --- 4. 급여 ---------------------------------------------------------------------


def _rows(*items):
    return lambda *a: [{"id": f"a{i}", "status": "approved", **row} for i, row in enumerate(items)]


def test_hourly_uses_attendance_minutes_with_33_withholding_only(monkeypatch):
    sign(freelancer("hourly", 12500))
    monkeypatch.setattr(svc, "_payroll_attendance_rows", _rows({"work_date": "2026-11-02", "worked_minutes": 600}))
    draft = svc.payroll_draft_from_attendance(
        {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}, ADMIN)
    assert draft["gross_pay"] == 125000
    assert draft["employment_tax_type"] == "freelancer_33"
    assert draft["tax_withholding"] == 4120 and draft["insurance_deduction"] == 0
    codes = {item["code"] for item in draft["deduction_items"]}
    assert codes == {"income_tax", "local_income_tax"}
    note = draft["pay_items"][0]["calc_note"]
    assert "시급 12,500원" in note and "승인 600분" in note


def test_daily_uses_distinct_approved_days(monkeypatch):
    sign(freelancer("daily", 80000))
    monkeypatch.setattr(svc, "_payroll_attendance_rows", _rows(
        {"work_date": "2026-11-02", "worked_minutes": 300}, {"work_date": "2026-11-02", "worked_minutes": 120},
        {"work_date": "2026-11-03", "worked_minutes": 480}))
    draft = svc.payroll_draft_from_attendance(
        {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}, ADMIN)
    assert draft["gross_pay"] == 160000
    assert "일급 80,000원" in draft["pay_items"][0]["calc_note"] and "승인 2일" in draft["pay_items"][0]["calc_note"]
    assert draft["insurance_deduction"] == 0


def test_legacy_hourly_signed_contract_still_computes_from_attendance(monkeypatch):
    make_legacy(sign(freelancer("hourly", 13000))["contract"]["id"])
    monkeypatch.setattr(svc, "_payroll_attendance_rows", _rows({"work_date": "2026-11-02", "worked_minutes": 600}))
    draft = svc.payroll_draft_from_attendance(
        {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}, ADMIN)
    assert draft["gross_pay"] == 130000 and draft["tax_withholding"] == 4290


def test_monthly_applies_contract_amount_without_manual_gross():
    sign(freelancer("monthly", 2000000))
    draft = svc.payroll_draft_from_contract(
        {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}, ADMIN)
    assert draft["gross_pay"] == 2000000
    assert draft["employment_tax_type"] == "freelancer_33"
    assert draft["non_tax_meal_allowance"] == 0 and draft["insurance_deduction"] == 0
    assert draft["tax_withholding"] == 66000
    assert "월 2,000,000원" in draft["pay_items"][0]["calc_note"]


def test_lump_stays_manual_and_hourly_daily_refuse_contract_amount():
    sign(freelancer("lump", 500000))
    request = {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_contract(dict(request), ADMIN)
    assert exc.value.status_code == 400
    ok = svc.payroll_draft_from_contract({**request, "gross_pay": 300000, "amount_confirmed": True}, ADMIN)
    assert ok["gross_pay"] == 300000


@pytest.mark.parametrize("basis", ["hourly", "daily"])
def test_attendance_bases_do_not_take_contract_amount(basis):
    sign(freelancer(basis, 12500))
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_contract(
            {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}, ADMIN)
    assert exc.value.status_code == 400


def test_attendance_draft_refuses_monthly_and_lump(monkeypatch):
    sign(freelancer("monthly", 2000000))
    monkeypatch.setattr(svc, "_payroll_attendance_rows", _rows({"work_date": "2026-11-02", "worked_minutes": 600}))
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance(
            {"payroll_month": "2026-11", "employee_request_id": "join-mia", "business_id": "biz-mia"}, ADMIN)
    assert exc.value.status_code == 400


def test_payroll_defaults_expose_fee_basis():
    sign(freelancer("monthly", 2000000))
    plan = svc.payroll_defaults("member@example.com", "2026-11", ADMIN, "join-mia")
    assert plan["fee_basis"] == "monthly"
    assert plan["defaults"]["gross_pay"] == 2000000 and plan["defaults"]["employment_tax_type"] == "freelancer_33"


def test_employment_monthly_calculation_unchanged():
    derived = {"contract_id": "c9", "employment": {
        "contract_type": "regular", "wage_type": "monthly", "wage": 2800000, "non_tax_meal_allowance": 200000,
        "employment_tax_type": "four_insurance"}}
    plan = svc._payroll_defaults_from_employment(derived)
    assert plan["defaults"]["gross_pay"] == 2800000
    assert plan["defaults"]["taxable_pay"] == 2600000 and plan["defaults"]["non_tax_meal_allowance"] == 200000
    assert "fee_basis" not in plan


def test_held_amount_and_support_work_fields_do_not_break_freelancer_fee_basis_draft():
    # D안(보류 금액)·지원근무 코드가 프리랜서 시급 경로에서 쓰는 필드가 그대로 있어야 한다.
    source = (REPO / "app/services/yeoljeong_finance_service.py").read_text(encoding="utf-8")
    for token in ("held_release_amount", "held_items", "report_status", "unreported_reason"):
        assert token in source


# --- 5. 경고 --------------------------------------------------------------------


def test_min_wage_warning_is_server_side_and_non_blocking():
    assert svc.MIN_WAGE_2026 == 10320
    warnings = svc.freelancer_fee_warnings("freelancer", "case_fee", "hourly", 9000)
    assert warnings and "10,320원" in warnings[0] and "9,000원" in warnings[0]
    assert svc.freelancer_fee_warnings("freelancer", "case_fee", "hourly", 12500) == []
    assert svc.freelancer_fee_warnings("freelancer", "case_fee", "daily", 100) == []
    saved = svc.save_contract(freelancer("hourly", 9000), ADMIN)
    assert saved["pay_basis_warnings"] == warnings
    assert "pay_basis_warnings" not in stored(saved["id"])


def test_labor_risk_notice_rules():
    assert svc.freelancer_labor_risk_notice("freelancer", "case_fee", "hourly") == LABOR_NOTICE
    assert svc.freelancer_labor_risk_notice("freelancer", "case_fee", "daily") == LABOR_NOTICE
    assert svc.freelancer_labor_risk_notice("freelancer", "hourly", None) == LABOR_NOTICE
    assert svc.freelancer_labor_risk_notice("freelancer", "case_fee", "monthly") == ""
    assert svc.freelancer_labor_risk_notice("freelancer", "case_fee", "lump", {"weekdays": [0, 2]}) == LABOR_NOTICE
    assert svc.freelancer_labor_risk_notice("part_time", "hourly", None, {"weekdays": [0]}) == ""


def test_contract_list_and_payroll_rows_carry_notice_text():
    sign(freelancer("hourly", 12500))
    listed = svc.list_contracts(ADMIN)
    assert any(row.get("labor_risk_notice") == LABOR_NOTICE for row in listed)
    employees = svc.list_approved_employees(ADMIN)
    assert employees[0]["labor_risk_notice"] == LABOR_NOTICE


# --- 6. 근무 요일 설정 -----------------------------------------------------------


def test_work_pattern_roundtrip_gaps_and_notice(monkeypatch):
    sign(freelancer("lump", 500000))
    assert svc.get_employee_work_pattern("join-mia", ADMIN)["labor_risk_notice"] == ""
    saved = svc.set_employee_work_pattern(
        "join-mia", {"weekdays": [0, 2, 4], "start_time": "10:00", "end_time": "14:00"}, ADMIN)
    assert saved["work_pattern"]["weekdays"] == [0, 2, 4]
    assert saved["labor_risk_notice"] == LABOR_NOTICE
    assert svc.get_employee_work_pattern("join-mia", ADMIN)["work_pattern"]["start_time"] == "10:00"
    monkeypatch.setattr(svc, "_payroll_attendance_rows", _rows(
        {"work_date": "2026-11-02", "worked_minutes": 240, "check_in_at": "2026-11-02T10:00:00+09:00"}))
    gaps = svc.employee_work_pattern_gaps("join-mia", "2026-11", ADMIN)
    assert "2026-11-02" not in gaps["missing_days"]
    assert "2026-11-04" in gaps["missing_days"] and "2026-11-03" not in gaps["missing_days"]
    with pytest.raises(HTTPException):
        svc.set_employee_work_pattern("join-mia", {"weekdays": [9]}, ADMIN)


def test_work_pattern_routes_are_whitelisted_for_owner_contract_scope():
    from app.core import obys_tenant

    source = (REPO / "app/api/obys_finance.py").read_text(encoding="utf-8")
    assert "/employees/approved/{request_id}/work-pattern" in source
    assert "/employees/approved/{request_id}/work-pattern/gaps" in source
    whitelist = repr(obys_tenant._OWNER_CONTRACT_WHITELIST)
    assert "work-pattern" in whitelist


# --- 7. 화면(Node) ---------------------------------------------------------------

NODE_SCRIPT = r"""
globalThis.window = globalThis;
globalThis.location = { href: "https://fb.example/x", origin: "https://fb.example", search: "" };
const [coreArg, autoArg] = process.argv.slice(-2);
require(coreArg);
const A = require(autoArg);
const C = window.ObysContractCore;
const base = {
  employeeRequestId: "r1", employeeName: "정윤선", employeeEmail: "a@b.c", employeePhone: "010-0000-0000",
  employeeBirthDate: "1977-01-01", employeeAddress: "주소", businessId: "b1", branch: "본점",
  contractDate: "2026-11-02", contractType: "freelancer", employmentTaxType: "freelancer_33", wageType: "case_fee",
  startDate: "2026-11-02", workplace: "매장", jobDescription: "매장 지원 용역",
  freelancerScope: "범위", freelancerSettlementTerms: "정산", terminationTerms: "해지", confidentialityTerms: "비밀",
  employerName: "상호", employerRegistrationNo: "000-00-00000", employerRepresentative: "대표", employerAddress: "주소",
  workDays: "월,화", workTime: "10:00~14:00", weeklyHours: "", wage: 12500, feeRate: 12500,
};
const check = extra => C.checkDraft({ ...base, ...extra });
const out = { bases: {}, missing: {}, warn: {}, notice: {}, auto: {} };
for (const b of ["lump", "hourly", "daily", "monthly"]) {
  out.bases[b] = check({ feeBasis: b });
  out.missing[b] = check({ feeBasis: b, wage: 0, feeRate: 0 });
  out.notice[b] = C.laborRiskNotice("freelancer", b, []);
  out.auto[b] = A.compute({ contractType: "freelancer", wageType: "case_fee", feeBasis: b, wage: "12500", startDate: "2026-11-02", term: "none" });
}
out.warn.low = C.feeWarnings("freelancer", "hourly", 9000);
out.warn.ok = C.feeWarnings("freelancer", "hourly", 12500);
out.warn.daily = C.feeWarnings("freelancer", "daily", 100);
out.legacy = C.feeBasisOf("freelancer", "hourly", "");
out.employmentHourly = C.feeBasisOf("part_time", "hourly", "");
out.formula = C.feeFormula("hourly", 12500);
out.monthlyLegacy = check({ wageType: "monthly", feeBasis: "" });
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경")
    done = subprocess.run(
        [node, "-e", NODE_SCRIPT, "--", str(MODULES / "contract-core.js"), str(MODULES / "contract-autofill.js")],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


def _ok(result):
    return bool(result.get("ok", not result.get("errors") and not result.get("blockers"))) if isinstance(result, dict) else bool(result)


def test_screen_accepts_four_bases_and_labels_missing_rate(js):
    for basis in ("lump", "hourly", "daily", "monthly"):
        assert js["bases"][basis] == js["bases"]["lump"] or _ok(js["bases"][basis])
        assert js["missing"][basis] != js["bases"][basis]
    label = {"hourly": "시급", "daily": "일급", "monthly": "월 용역비", "lump": "용역비"}
    for basis, word in label.items():
        assert word in json.dumps(js["missing"][basis], ensure_ascii=False)


def test_screen_and_server_share_min_wage_warning(js):
    server = svc.freelancer_fee_warnings("freelancer", "case_fee", "hourly", 9000)
    assert js["warn"]["low"] == server
    assert js["warn"]["ok"] == [] and js["warn"]["daily"] == []


def test_screen_labor_notice_text_matches_server(js):
    assert js["notice"]["hourly"] == js["notice"]["daily"] == LABOR_NOTICE
    assert js["notice"]["lump"] == js["notice"]["monthly"] == ""


def test_screen_reads_legacy_hourly_and_keeps_employment_unchanged(js):
    assert js["legacy"] == "hourly" and js["employmentHourly"] == ""
    assert js["formula"] == "시간당 12,500원 × 승인된 수행 시간"


def test_screen_still_rejects_monthly_wage_type_for_freelancer(js):
    assert js["monthlyLegacy"] != js["bases"]["lump"]


def _src(name):
    return (MODULES / name).read_text(encoding="utf-8")


def test_notice_is_visible_text_not_title_only():
    for name in ("contract-editor-v41.js", "payroll-admin.js"):
        source = _src(name)
        assert "labor_risk_notice" in source or "laborRiskNotice" in source or "LABOR_RISK" in source
    assert "data-payroll-labor-risk" in _src("payroll-admin.js")
    assert 'showLaborRisk' in _src("payroll-admin.js")
    assert "textContent" in _src("payroll-admin.js").split("const showLaborRisk")[1].split("\n")[0]
    assert "title=" not in _src("payroll-admin.js").split("const showLaborRisk")[1].split("\n")[0]
    editor = _src("contract-editor-v41.js")
    assert "feeNoticeHtml" in editor
    notice_fn = editor.split("function feeNoticeHtml")[1].split("function amendNoteHtml")[0]
    assert "title=" not in notice_fn and "role=\"status\"" in notice_fn


def test_payroll_admin_branches_by_fee_basis_and_never_forces_gross():
    source = _src("payroll-admin.js")
    block = source[source.index('if (action === "settlement")'):source.index('if (action === "draft")')]
    assert 'feeBasis === "hourly" || feeBasis === "daily"' in block and "/payroll/draft-from-attendance" in block
    monthly = block[block.index('feeBasis === "monthly"'):block.index("const gross = form.elements.gross_pay.value")]
    assert "gross_pay" not in monthly and "amount_confirmed" not in monthly and "/payroll/draft-from-contract" in monthly


def test_work_pattern_module_preserves_unsaved_input():
    source = _src("work-pattern.js")
    assert "drafts.set(" in source and "drafts.get(state.key)" in source and "drafts.delete(state.key)" in source
    assert "work-pattern" in source and "window.ObysWorkPattern" in source
    html = (REPO / "app/static/apps/obys/admin.html").read_text(encoding="utf-8")
    assert "modules/work-pattern.js" in html and "ObysWorkPattern.mount" in html and 'id="workPatternHost"' in html
    contract_body = _src("contract-core.js")
    assert "work_pattern" not in contract_body and "workPattern" not in contract_body


def test_old_screen_index_html_keeps_required_markers_and_has_basis_select():
    html = (REPO / "app/static/apps/obys/index.html").read_text(encoding="utf-8")
    assert "feeBasis" in html and "FREELANCER_HOURLY_AUTO_TEXT_RE" in html and "autoHourly" not in html
    assert html.index("syncFreelancerWageOptions(") < html.index("els.contractForm.wageType.value")


def test_delivery_restaurant_scope_is_retained():
    editor = _src("contract-editor-v41.js") + _src("contract-autofill.js") + _src("contract-core.js")
    html = (REPO / "app/static/apps/obys/index.html").read_text(encoding="utf-8")
    service = (REPO / "app/services/yeoljeong_finance_service.py").read_text(encoding="utf-8")
    assert "배달" in editor + html + service

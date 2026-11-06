"""오비서 급여 — 보류 금액(지급 예정, 공제 아님) + 신고 미처리 표시 + 보류금 지급·충당 원장.

파일 모드로만 돈다. 보류 금액은 공제가 아니라 직원에게 줄 돈이다 — 공제 항목·법정 공제 합계에 들어가지 않고
실수령액(net_pay)에서만 빠진다. 보류 잔액은 음수가 될 수 없다.
"""
import os
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service
from app.services import yeoljeong_payroll_pdf as pdf

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
WARNING = "신고 미처리 · 노무·세무 확인 필요"
HOLD = [{"amount": 330000, "reason": "신고 지연으로 보류"}]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")

    def disable_db(coroutine):
        close = getattr(coroutine, "close", None)
        if close:
            close()

    monkeypatch.setattr(svc, "_run_db", disable_db)
    clock = {"now": datetime(2026, 11, 6, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)

    async def fake_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-1", "name": "가입 직원", "email": EMPLOYEE["email"], "tenant_id": TENANT,
        "business_id": "biz-1", "branch": "본점", "status": "approved",
    }])


def draft(month="2026-10", gross=3000000, tax=0, insurance=0, **extra):
    deductions = [
        *([{"code": "income_tax", "label": "소득세", "amount": tax}] if tax else []),
        *([{"code": "national_pension", "label": "국민연금", "amount": insurance}] if insurance else []),
    ]
    payload = {
        "employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-1", "branch": "본점",
        "payroll_month": month, "pay_date": f"{month}-10", "gross_pay": gross, "tax_withholding": tax,
        "insurance_deduction": insurance, "other_deduction": 0,
        "pay_items": [{"code": "base", "label": "기본급", "amount": gross, "calc_note": "월 기본급"}],
        "deduction_items": deductions,
    }
    payload.update(extra)
    return svc.save_payroll(payload, ADMIN)


def confirmed(**kwargs):
    return svc.confirm_payroll(draft(**kwargs)["id"], ADMIN)


def held_statement(month="2026-10", **kwargs):
    base = {"report_status": "unreported", "unreported_reason": "4대보험 신고 전", "held_items": HOLD}
    base.update(kwargs)
    return confirmed(month=month, **base)


def pdf_text(data):
    from pdfminer.high_level import extract_text

    return " ".join(extract_text(BytesIO(data)).split())


def listed(month):
    return {row["id"]: row for row in svc.list_payroll(ADMIN, month)}


# --- 1. 명세 필드와 실수령액 ------------------------------------------------------


def test_hold_reduces_net_pay_but_is_not_a_deduction():
    row = draft(report_status="unreported", unreported_reason="4대보험 신고 전", held_items=HOLD)
    assert row["net_pay"] == 2670000 and row["gross_pay"] == 3000000
    assert row["tax_withholding"] == row["insurance_deduction"] == row["other_deduction"] == 0
    assert not row.get("deduction_items")
    assert row["held_amount"] == 330000 and row["report_status"] == "unreported"
    totals = pdf.payroll_totals(row)
    assert totals["total_deductions"] == 0 and totals["held_amount"] == 330000
    assert totals["net_pay"] == 2670000 and totals["consistent"] is True


def test_hold_is_not_recorded_in_any_deduction_or_tax_item():
    row = draft(tax=100000, insurance=200000, report_status="unreported", unreported_reason="확인 중", held_items=HOLD)
    assert {item["code"] for item in row["deduction_items"]} == {"income_tax", "national_pension"}
    assert sum(item["amount"] for item in row["deduction_items"]) == 300000
    assert pdf.payroll_totals(row)["total_deductions"] == 300000
    assert row["net_pay"] == 3000000 - 300000 - 330000
    assert "held_ratio" not in row and "ratio" not in str(row.get("held_items"))


def test_report_status_defaults_to_reported_and_legacy_rows_are_unchanged():
    row = draft(tax=100000, insurance=200000)
    assert row["net_pay"] == 2700000 and pdf.payroll_totals(row)["held_amount"] == 0 and pdf.payroll_totals(row)["report_status"] == "reported"
    assert "held_items" not in row and "held_amount" not in row and "held_balance" not in row


def test_unreported_requires_reason_and_hold_items_are_validated():
    for extra in (
        {"report_status": "unreported"},
        {"report_status": "unreported", "unreported_reason": "   "},
        {"report_status": "pending"},
        {"held_items": [{"amount": 1000, "reason": ""}]},
        {"held_items": [{"amount": 0, "reason": "사유"}]},
        {"held_items": [{"amount": "12.5", "reason": "사유"}]},
        {"held_items": [{"amount": 1000, "reason": "사유"}] * 11},
    ):
        with pytest.raises(HTTPException) as exc:
            draft(**extra)
        assert exc.value.status_code == 400 and any("가" <= ch <= "힣" for ch in exc.value.detail)
    assert draft(report_status="unreported", unreported_reason="노무 확인 중")["report_status"] == "unreported"


def test_client_cannot_forge_derived_hold_fields_or_release_lines():
    forged = draft(
        held_items=HOLD, held_amount=1, held_applied=330000, held_balance=999, held_origin_unreported=True,
        pay_items=[
            {"code": "base", "label": "기본급", "amount": 3000000, "calc_note": "월 기본급"},
            {"code": "held_release", "label": "보류금 지급", "amount": 500000, "source": "held_release"},
        ],
        gross_pay=3500000,
    )
    assert forged["gross_pay"] == 3000000 and forged["net_pay"] == 2670000
    assert forged["held_amount"] == 330000 and "held_applied" not in forged
    assert all(item["code"] != "held_release" for item in forged["pay_items"])
    assert forged["held_balance"] == 330000 and "held_origin_unreported" not in forged


def test_net_pay_cannot_go_negative():
    with pytest.raises(HTTPException) as exc:
        draft(gross=100000, held_items=[{"amount": 200000, "reason": "과다 보류"}])
    assert exc.value.status_code == 400


# --- 2. 누계 잔액 ------------------------------------------------------------------


def test_cumulative_balance_over_months_per_employee():
    first = held_statement("2026-09")
    second = held_statement("2026-10", held_items=[{"amount": 100000, "reason": "추가 보류"}])
    third = confirmed(month="2026-11")
    rows = {**listed("2026-09"), **listed("2026-10"), **listed("2026-11")}
    for statement in (first, second, third):
        assert rows[statement["id"]]["held_balance_total"] == 430000
    assert first["held_balance"] == 330000 and second["held_balance"] == 430000
    assert "held_balance" not in third or third["held_balance"] == 430000


def test_unconfirmed_draft_does_not_count_in_balance():
    draft(held_items=HOLD)
    assert all("held_balance_total" not in row for row in svc.list_payroll(ADMIN))


# --- 3. 해제 (a) 이후 명세의 보류금 지급 ---------------------------------------------


def test_later_statement_releases_held_balance_as_pay_line():
    held_statement("2026-09")
    released = confirmed(month="2026-10", held_release_amount=100000, tax=100000, insurance=200000)
    line = next(item for item in released["pay_items"] if item["code"] == "held_release")
    assert line["amount"] == 100000 and line["source"] == "held_release" and line["label"] == "보류금 지급"
    assert released["gross_pay"] == 3100000 and released["net_pay"] == 3100000 - 300000
    assert released["held_balance"] == 230000
    assert pdf.payroll_totals(released)["held_release"] == 100000 and pdf.payroll_totals(released)["consistent"] is True
    assert listed("2026-10")[released["id"]]["held_balance_total"] == 230000


def test_release_is_excluded_from_taxable_basis():
    held_statement("2026-09")
    released = confirmed(
        month="2026-10", held_release_amount=50000, employment_tax_type="four_insurance", meal_provision="cash_no_meal",
        taxable_pay=3000000, non_tax_meal_allowance=0,
    )
    assert released["taxable_pay"] == 3000000 and released["gross_pay"] == 3050000
    basis = released.get("deduction_basis") or {}
    assert "3050000" not in str(basis.get("taxable_pay", ""))


def test_release_above_balance_is_409_and_nothing_is_written():
    held_statement("2026-09")
    before = len(svc._read_hr("payroll_statements", ADMIN))
    with pytest.raises(HTTPException) as exc:
        draft(month="2026-10", held_release_amount=330001)
    assert exc.value.status_code == 409
    assert len(svc._read_hr("payroll_statements", ADMIN)) == before


def test_release_before_the_hold_month_is_409():
    held_statement("2026-10")
    with pytest.raises(HTTPException) as exc:
        draft(month="2026-09", held_release_amount=1000)
    assert exc.value.status_code == 409


def test_two_drafts_cannot_both_release_the_same_balance():
    held_statement("2026-09")
    first = draft(month="2026-10", held_release_amount=300000)
    second = draft(month="2026-11", held_release_amount=300000)
    svc.confirm_payroll(first["id"], ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(second["id"], ADMIN)
    assert exc.value.status_code == 409


def test_lowering_an_earlier_hold_cannot_strand_a_later_release():
    first = held_statement("2026-09")
    confirmed(month="2026-10", held_release_amount=300000)
    with pytest.raises(HTTPException) as exc:
        draft(id=first["id"], month="2026-09", report_status="unreported", unreported_reason="축소",
              held_items=[{"amount": 100000, "reason": "축소"}])
    assert exc.value.status_code == 409


# --- 4. 정정 (b) 신고 처리로 전환 -----------------------------------------------------


def correct_to_reported(original, **extra):
    saved = draft(id=original["id"], month=original["payroll_month"], tax=100000, insurance=200000,
                  report_status="reported", held_items=HOLD, **extra)
    return svc.confirm_payroll(saved["id"], ADMIN)


def test_conversion_to_reported_applies_hold_to_statutory_then_pays_remainder():
    original = held_statement("2026-10")
    paid = svc.pay_payroll(original["id"], ADMIN)
    assert paid["net_pay"] == 2670000 and paid["payout"]["amount"] == 2670000
    fixed = correct_to_reported(original)
    assert fixed["version"] == 2 and fixed["report_status"] == "reported" and "unreported_reason" not in fixed
    assert fixed["held_amount"] == 330000 and fixed["held_applied"] == 300000
    line = next(item for item in fixed["pay_items"] if item["code"] == "held_release")
    assert line["amount"] == 30000 and line["source"] == "held_release"
    assert fixed["gross_pay"] == 3030000 and fixed["net_pay"] == 3030000 - 300000 - 330000 + 300000 == 2700000
    assert fixed["held_balance"] == 0
    assert pdf.payroll_totals(fixed)["total_deductions"] == 300000 and pdf.payroll_totals(fixed)["consistent"] is True
    assert listed("2026-10")[fixed["id"]]["held_balance_total"] == 0


def test_conversion_payout_difference_uses_net_after_hold():
    original = held_statement("2026-10")
    svc.pay_payroll(original["id"], ADMIN)
    fixed = correct_to_reported(original)
    state = svc.payroll_settlement_states(ADMIN)[fixed["id"]]
    assert state["pending_amount"] == -30000
    assert svc.payroll_payout_compare({**fixed, "payout": {"amount": 2670000}})["diff_amount"] == -30000


def test_conversion_where_statutory_exceeds_hold_applies_only_the_hold():
    original = held_statement("2026-10", held_items=[{"amount": 100000, "reason": "일부 보류"}])
    saved = draft(id=original["id"], month="2026-10", tax=100000, insurance=200000, report_status="reported",
                  held_items=[{"amount": 100000, "reason": "일부 보류"}])
    fixed = svc.confirm_payroll(saved["id"], ADMIN)
    assert fixed["held_applied"] == 100000 and not any(item["code"] == "held_release" for item in fixed["pay_items"])
    assert fixed["net_pay"] == 3000000 - 300000 - 100000 + 100000 and fixed["held_balance"] == 0


def test_conversion_plus_excess_release_is_409():
    original = held_statement("2026-10")
    with pytest.raises(HTTPException) as exc:
        draft(id=original["id"], month="2026-10", tax=100000, insurance=200000, report_status="reported",
              held_items=HOLD, held_release_amount=1)
    assert exc.value.status_code == 409


def test_resaving_a_converted_statement_keeps_the_same_numbers():
    original = held_statement("2026-10")
    fixed = correct_to_reported(original)
    again = draft(id=fixed["id"], month="2026-10", tax=100000, insurance=200000, report_status="reported", held_items=HOLD)
    assert again["id"] == fixed["id"] and again["version"] == 2
    assert again["net_pay"] == 2700000 and again["held_applied"] == 300000


# --- 5. 확정 후 동결 ----------------------------------------------------------------


def test_confirmed_hold_is_frozen_and_edits_create_a_new_version():
    original = held_statement("2026-10")
    same = draft(id=original["id"], month="2026-10", report_status="unreported", unreported_reason="4대보험 신고 전", held_items=HOLD)
    assert same["id"] == original["id"] and same["version"] == 1
    edited = draft(id=original["id"], month="2026-10", report_status="unreported", unreported_reason="4대보험 신고 전",
                   held_items=[{"amount": 200000, "reason": "금액 정정"}])
    assert edited["id"] != original["id"] and edited["version"] == 2 and edited["supersedes_id"] == original["id"]
    assert edited["status"] == "draft" and edited["net_pay"] == 2800000
    kept = next(row for row in svc._read_hr("payroll_statements", ADMIN) if row["id"] == original["id"])
    assert kept["held_items"] == HOLD and kept["net_pay"] == 2670000 and kept["status"] == "confirmed"


def test_changing_only_report_status_creates_a_new_version():
    original = held_statement("2026-10")
    edited = draft(id=original["id"], month="2026-10", report_status="reported", held_items=HOLD)
    assert edited["id"] != original["id"] and edited["version"] == 2


def test_later_balance_follows_only_the_latest_confirmed_version():
    original = held_statement("2026-09")
    saved = draft(id=original["id"], month="2026-09", report_status="unreported", unreported_reason="정정",
                  held_items=[{"amount": 200000, "reason": "금액 정정"}])
    svc.confirm_payroll(saved["id"], ADMIN)
    assert listed("2026-09")[saved["id"]]["held_balance_total"] == 200000


# --- 6. PDF ------------------------------------------------------------------------


def test_pdf_rows_and_text_show_hold_balance_and_unreported_warning():
    row = held_statement("2026-10")
    labels = dict(pdf.payroll_pdf_rows(row))
    assert labels["신고 상태"] == WARNING and labels["미처리 사유"] == "4대보험 신고 전"
    assert labels["보류 금액 합계(지급 예정, 공제 아님)"] == "330,000원"
    assert labels["보류 잔액(누계, 지급 예정)"] == "330,000원"
    assert labels["공제 합계"] == "0원" and labels["실지급액"] == "2,670,000원"
    data, _ = svc.payroll_pdf_for_download(row["id"], ADMIN)
    text = pdf_text(data)
    assert data.startswith(b"%PDF") and WARNING in text
    assert "보류 금액(지급 예정, 공제 아님)" in text and "보류 잔액" in text and "2,670,000원" in text


def test_pdf_of_reported_statement_has_no_warning_or_hold_section():
    row = confirmed(tax=100000, insurance=200000)
    labels = dict(pdf.payroll_pdf_rows(row))
    assert "신고 상태" not in labels and not any("보류" in label for label in labels)
    assert WARNING not in pdf_text(svc.payroll_pdf_for_download(row["id"], ADMIN)[0])


def test_pdf_refuses_when_stored_net_ignores_the_hold():
    row = held_statement("2026-10")
    with pytest.raises(pdf.PayrollPdfError):
        pdf.render_payroll_statement_pdf({**row, "net_pay": 3000000})


def test_freelancer_pdf_shows_hold_without_insurance_wording():
    row = confirmed(
        employment_tax_type="freelancer_33", gross=1000000, report_status="unreported", unreported_reason="사업소득 신고 전",
        held_items=[{"amount": 110000, "reason": "신고 지연 보류"}],
        deduction_items=[{"code": "income_tax", "label": "사업소득세(3%)", "amount": 30000},
                         {"code": "local_income_tax", "label": "지방소득세(0.3%)", "amount": 3000}],
        tax_withholding=33000,
    )
    labels = dict(pdf.payroll_pdf_rows(row))
    assert labels["신고 상태"] == WARNING and labels["보류 금액 합계(지급 예정, 공제 아님)"] == "110,000원"
    assert labels["실지급액"] == f"{1000000 - 33000 - 110000:,}원"
    assert not any("4대보험" in label for label in labels)


# --- 7. 지급 차액 ---------------------------------------------------------------------


def test_payout_difference_is_computed_against_net_after_hold():
    row = held_statement("2026-10")
    exact = svc.pay_payroll(row["id"], ADMIN)
    assert svc.payroll_payout_compare(exact)["diff_amount"] == 0
    other = held_statement("2026-09")
    under = svc.pay_payroll(other["id"], ADMIN, paid_amount=2600000)
    compare = svc.payroll_payout_compare(under)
    assert compare["base_net"] == 2670000 and compare["diff_amount"] == -70000


# --- 8. 직원·다른 직원·API 경계 ------------------------------------------------------------


def test_balance_is_per_employee():
    held_statement("2026-09")
    svc._write("employee_join_requests", [
        *svc._read_hr("employee_join_requests", ADMIN),
        {"id": "join-2", "name": "다른 직원", "email": "other@example.com", "tenant_id": TENANT,
         "business_id": "biz-1", "branch": "본점", "status": "approved"},
    ])
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll({
            "employee_email": "other@example.com", "employee_name": "다른 직원", "business_id": "biz-1", "branch": "본점",
            "payroll_month": "2026-10", "pay_date": "2026-10-10", "gross_pay": 3000000, "tax_withholding": 0,
            "insurance_deduction": 0, "other_deduction": 0, "held_release_amount": 1000,
            "pay_items": [{"code": "base", "label": "기본급", "amount": 3000000, "calc_note": ""}],
        }, ADMIN)
    assert exc.value.status_code == 409


def test_employee_list_has_no_ledger_balance_of_other_months():
    held_statement("2026-09")
    rows = svc.list_payroll(EMPLOYEE)
    assert rows and all("held_balance_total" not in row for row in rows)


# --- 관리자 화면(admin.html · payroll-admin.js) ------------------------------------------

import json
import re
import shutil
import subprocess
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html"
ADMIN_JS = Path(__file__).resolve().parents[2] / "app/static/apps/obys/modules/payroll-admin.js"
UI_NAMES = (
    "payrollPayloadV41", "payrollNumV41", "payrollDeductionTotalV41", "payrollContractIdV41", "payrollUnsplitV41",
    "payrollPaymentCheckV41", "payrollFlagsV41", "payrollPayoutV41", "payrollPayoutBadgeV41", "payrollPayoutOpenCountV41",
    "payrollPayoutPanelV41", "payrollSummaryV41", "payrollSummaryHtmlV41", "payrollBasisHtmlV41",
)


def _ui(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    source = ADMIN_HTML.read_text(encoding="utf-8")

    def fn(name):
        start = source.index(f"    function {name}(")
        return source[start:source.index("\n", start)]

    prelude = "const escapeHtml=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');\n"
    out = subprocess.run([node, "-e", prelude + "\n".join(fn(n) for n in UI_NAMES) + "\n" + body], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


UI_ROWS = """
const mk=raw=>({__live:{raw}});
const held={status:'confirmed',gross_pay:3000000,tax_withholding:0,insurance_deduction:0,other_deduction:0,net_pay:2670000,employee_email:'a@x.com',
  report_status:'unreported',held_amount:330000,held_balance_total:330000,held_employee_key:'a@x.com',
  statement_payload:{report_status:'unreported',unreported_reason:'신고 지연',held_items:[{amount:330000,reason:'신고 지연으로 보류'}],source_contract_id:'c1'}};
const other={status:'confirmed',gross_pay:3000000,net_pay:3000000,employee_email:'b@x.com',statement_payload:{source_contract_id:'c2'}};
const dup={...held,statement_payload:{...held.statement_payload}};
const rows=[mk(held),mk(other),mk(dup)];
"""


def test_ui_badge_shows_full_warning_as_screen_text():
    flags = _ui(UI_ROWS + "console.log(JSON.stringify(rows.map(payrollFlagsV41)))")
    assert re.search(r">\s*신고 미처리 · 노무·세무 확인 필요\s*</small>", flags[0])
    assert "330,000원" in flags[0] and "공제 아님" in flags[0]
    assert flags[1] == ""


def test_ui_summary_cards_count_unreported_held_and_unique_balance():
    html = _ui(UI_ROWS + "console.log(JSON.stringify(payrollSummaryHtmlV41(rows,'2026-10')))")
    assert "신고 미처리" in html and "2건" in html and "신고 미처리 · 노무·세무 확인 필요" in html
    assert "누적 보류 잔액 330,000원(1명)" in html


def test_ui_summary_totals_unchanged_for_legacy_rows():
    got = _ui(UI_ROWS + "console.log(JSON.stringify(payrollSummaryV41([mk(other)])))")
    assert set(got) == {"count", "gross", "deductions", "net", "confirmed", "pending", "noContract", "unsplit"}


def test_ui_basis_lists_reason_items_and_balance():
    html = _ui(UI_ROWS + "console.log(JSON.stringify(payrollBasisHtmlV41(rows[0])))")
    assert "신고 지연" in html and "신고 지연으로 보류" in html and "보류 금액(지급 예정, 공제 아님)" in html
    assert "누적 보류 잔액" in html and "330,000원" in html


def test_admin_js_has_selector_reason_hold_button_and_no_hardcoded_ratio():
    source = ADMIN_JS.read_text(encoding="utf-8")
    for needle in ("data-report-status", "data-report-reason", "data-held-add", "data-held-release", "report_status", "held_items", "held_release_amount"):
        assert needle in source
    assert "신고 미처리 · 노무·세무 확인 필요" in source
    assert not re.search(r"\b11\s*%|\*\s*0?\.11\b|/\s*100\s*\*\s*11\b", source)
    assert "held_ratio" not in source and "ratio:" not in source


def test_admin_js_syntax_and_exports():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    done = subprocess.run([node, "-e", f"globalThis.window=globalThis;const m=require({json.dumps(str(ADMIN_JS))});console.log(m.UNREPORTED_WARNING);console.log(m.isHeldLine({{code:'held_release'}}))"],
                          capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["신고 미처리 · 노무·세무 확인 필요", "true"]

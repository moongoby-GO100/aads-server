"""급여명세 PDF 법정 기재사항 — 임금명세서/용역비 지급명세서 분리, 항목별 금액, 3.3% 분리.

파일 모드로만 돈다. 운영 DB·외부 발송을 건드리지 않는다.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path
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
EMPLOYEE_EMAIL = "member@example.com"
MONTH = "2026-10"
RRN = re.compile(r"\d{6}\s*[-–]?\s*[1-8]\d{6}")


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
    clock = {"now": datetime(2026, 10, 20, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf-test", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE_EMAIL, "employee_no": "E-0007",
        "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
        "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])


PAY_ITEMS = [
    {"code": "base", "label": "기본급", "amount": 1900000, "calc_note": "월 209시간 × 시급 9,569원"},
    {"code": "overtime", "label": "연장근로수당", "amount": 300000, "calc_note": "연장 20시간 × 시급 9,569원 × 1.5"},
    {"code": "holiday", "label": "휴일근로수당", "amount": 100000, "calc_note": "휴일 8시간 × 시급 9,569원 × 1.5"},
    {"code": "meal", "label": "식대", "amount": 200000, "calc_note": "월 정액"},
]
DEDUCTION_ITEMS = [
    {"code": "national_pension", "label": "국민연금", "amount": 112500},
    {"code": "health_insurance", "label": "건강보험", "amount": 88650},
    {"code": "employment_insurance", "label": "고용보험", "amount": 22500},
    {"code": "income_tax", "label": "소득세", "amount": 40000},
    {"code": "local_income_tax", "label": "지방소득세", "amount": 4000},
]
WORK_SUMMARY = {"work_days": 22, "work_minutes": 12540, "overtime_minutes": 1200, "night_minutes": 0, "holiday_minutes": 480}


def wage_payload(**overrides):
    payload = {
        "employee_email": EMPLOYEE_EMAIL, "employee_name": "가입 직원", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "payroll_month": MONTH, "pay_date": "2026-11-10", "employment_tax_type": "four_insurance",
        "gross_pay": 2500000, "taxable_pay": 2300000, "non_tax_meal_allowance": 200000, "meal_provision": "cash_no_meal",
        "tax_withholding": 44000, "insurance_deduction": 223650, "other_deduction": 0,
        "pay_items": PAY_ITEMS, "deduction_items": DEDUCTION_ITEMS, "work_summary": WORK_SUMMARY,
    }
    payload.update(overrides)
    return payload


def freelancer_payload(gross, **overrides):
    tax = pdf.freelancer_withholding(gross)
    payload = {
        "employee_email": EMPLOYEE_EMAIL, "employee_name": "가입 직원", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "payroll_month": MONTH, "pay_date": "2026-11-10", "employment_tax_type": "freelancer_33",
        "gross_pay": gross, "taxable_pay": gross, "non_tax_meal_allowance": 0,
        "tax_withholding": tax["total"], "insurance_deduction": 0, "other_deduction": 0,
        "deduction_items": [
            {"code": "income_tax", "label": "소득세", "amount": tax["income_tax"]},
            {"code": "local_income_tax", "label": "지방소득세", "amount": tax["local_income_tax"]},
        ],
    }
    payload.update(overrides)
    return payload


def pdf_text(data):
    from pdfminer.high_level import extract_text

    return " ".join(extract_text(BytesIO(data)).split())


def confirmed(payload):
    return svc.save_payroll({**payload, "status": "confirmed"}, ADMIN)


def confirmed_import(payload, **marker):
    """과거 일괄 등록 명세: 초안 저장 뒤 서버 쪽에서 import 표식을 붙여 확정한다(클라이언트는 표식을 못 붙인다)."""
    draft = svc.save_payroll(payload, ADMIN)
    stored = next(row for row in svc._read_hr("payroll_statements", ADMIN) if row["id"] == draft["id"])
    svc._write_hr_record("payroll_statements", {**stored, **(marker or {"source_kind": "legacy_import"})}, ADMIN)
    return svc.confirm_payroll(draft["id"], ADMIN)


def labels(rows):
    return [label for label, _ in rows]


def row_value(rows, label):
    return next(value for name, value in rows if name == label)


# --- 임금명세서 ------------------------------------------------------------------------


def test_wage_rows_have_all_legal_items():
    saved = confirmed(wage_payload())
    rows = pdf.payroll_pdf_rows(saved)
    assert rows == pdf.wage_statement_rows(saved)
    assert row_value(rows, "성명") == "가입 직원"
    assert row_value(rows, "사원번호") == "E-0007"
    assert row_value(rows, "생년월일") == "1990-01-01"
    assert row_value(rows, "임금지급일") == "2026-11-10"
    assert row_value(rows, "임금 총액") == "2,500,000원"
    assert row_value(rows, "기본급") == "1,900,000원"
    assert row_value(rows, "연장근로수당") == "300,000원"
    assert row_value(rows, "계산방법 · 연장근로수당") == "연장 20시간 × 시급 9,569원 × 1.5"
    assert row_value(rows, "근로일수") == "22일"
    assert row_value(rows, "총 근로시간") == "209시간"
    assert row_value(rows, "연장근로시간") == "20시간"
    assert row_value(rows, "야간근로시간") == "0시간"
    assert row_value(rows, "휴일근로시간") == "8시간"
    assert row_value(rows, "공제 합계") == "267,650원"
    assert row_value(rows, "실지급액") == "2,232,350원"


def test_wage_rows_list_each_deduction_item_and_do_not_print_resident_number():
    saved = confirmed(wage_payload())
    rows = pdf.payroll_pdf_rows(saved)
    for item in DEDUCTION_ITEMS:
        assert row_value(rows, item["label"]) == f"{item['amount']:,}원"
    assert "4대보험 공제" not in labels(rows)
    flat = " ".join(f"{a} {b}" for a, b in rows)
    assert not RRN.search(flat)

    leaky = {**saved, "employee_name": "홍길동 900101-1234567", "employee_no": "9001011234567", "employee_birth_date": "900101-1234567"}
    leaky_rows = pdf.payroll_pdf_rows(leaky)
    assert not RRN.search(" ".join(f"{a} {b}" for a, b in leaky_rows))
    assert "생년월일" not in labels(leaky_rows)


def test_stored_item_text_with_resident_number_is_never_rendered():
    saved = confirmed(wage_payload())
    leaky_pay = [{**row, "calc_note": "산정 900101-1234567 기준"} if row["code"] == "base" else row for row in PAY_ITEMS]
    leaky_ded = [{**row, "label": "국민연금 900101-1234567"} if row["code"] == "national_pension" else row for row in DEDUCTION_ITEMS]
    for leaky in ({**saved, "pay_items": leaky_pay}, {**saved, "deduction_items": leaky_ded}):
        with pytest.raises(pdf.PayrollItemError):
            pdf.payroll_pdf_rows(leaky)
        with pytest.raises(pdf.PayrollPdfError):
            pdf.render_payroll_statement_pdf(leaky)


def test_zero_total_work_minutes_still_caps_overtime_night_holiday():
    with pytest.raises(pdf.PayrollItemError):
        pdf.normalize_payroll_items({"work_summary": {"work_minutes": 0, "holiday_minutes": 1}})
    assert pdf.normalize_payroll_items({"work_summary": {"work_minutes": 0, "overtime_minutes": 0}})["work_summary"]["work_minutes"] == 0
    assert pdf.normalize_payroll_items({"work_summary": {"overtime_minutes": 120}})["work_summary"] == {"overtime_minutes": 120}


def test_wage_pdf_text_matches_rows_and_has_title():
    saved = confirmed(wage_payload())
    text = pdf_text(pdf.render_payroll_statement_pdf(saved))
    assert "임금명세서" in text and "용역비 지급명세서" not in text
    for label, value in pdf.payroll_pdf_rows(saved):
        if label.startswith("계산방법 · "):
            assert value.split()[0] in text
            continue
        assert label in text and value in text
    assert not RRN.search(text)
    assert pdf.render_payroll_statement_pdf(saved) == pdf.render_payroll_statement_pdf(saved)


def test_wage_birth_date_is_omitted_when_record_has_none():
    rows = svc._read_hr("employee_join_requests", ADMIN)
    svc._write("employee_join_requests", [{**row, "birth_date": ""} for row in rows])
    saved = confirmed(wage_payload())
    assert "생년월일" not in labels(pdf.payroll_pdf_rows(saved))


# --- 항목 합계 검증 ----------------------------------------------------------------------


def test_pay_items_sum_must_match_gross_pay():
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(wage_payload(gross_pay=2400000, taxable_pay=2200000), ADMIN)
    assert exc.value.status_code == 400
    assert "지급 항목 합계" in exc.value.detail and "총지급액" in exc.value.detail


def test_deduction_items_sum_must_match_legacy_fields():
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(wage_payload(insurance_deduction=1), ADMIN)
    assert exc.value.status_code == 400 and "4대보험" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(wage_payload(tax_withholding=43000), ADMIN)
    assert exc.value.status_code == 400 and "소득세·지방소득세" in exc.value.detail


@pytest.mark.parametrize("bad", [
    {"pay_items": [{"code": "base", "amount": -1}]},
    {"pay_items": [{"code": "bogus", "amount": 1}]},
    {"pay_items": [{"code": "base", "amount": 1.5}]},
    {"pay_items": [{"code": "base"}]},
    {"pay_items": "x"},
    {"deduction_items": [{"code": "base", "amount": 1}]},
    {"work_summary": {"work_days": 40}},
    {"work_summary": {"unknown": 1}},
    {"work_summary": {"work_minutes": 60, "overtime_minutes": 120}},
    {"work_summary": {"work_minutes": 0, "overtime_minutes": 120}},
    {"work_summary": {"work_minutes": "0", "night_minutes": 1}},
    {"pay_items": [{"code": "base", "amount": 1, "calc_note": "홍길동 900101-1234567"}]},
    {"pay_items": [{"code": "base", "label": "9001011234567", "amount": 1}]},
    {"deduction_items": [{"code": "other", "label": "900101 1234567", "amount": 1}]},
])
def test_malformed_items_are_rejected_with_400(bad):
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(wage_payload(**bad), ADMIN)
    assert exc.value.status_code == 400


def test_blank_totals_are_derived_from_items():
    payload = wage_payload()
    for field in ("gross_pay", "tax_withholding", "insurance_deduction", "other_deduction"):
        payload.pop(field)
    payload.update(taxable_pay=0, non_tax_meal_allowance=0)
    saved = svc.save_payroll(payload, ADMIN)
    assert (saved["gross_pay"], saved["tax_withholding"], saved["insurance_deduction"], saved["other_deduction"]) == (2500000, 44000, 223650, 0)
    assert saved["net_pay"] == 2232350


def test_confirm_blocks_when_stored_items_drift_from_totals():
    saved = svc.save_payroll(wage_payload(), ADMIN)
    rows = svc._read_hr("payroll_statements", ADMIN)
    drifted = {**next(row for row in rows if row["id"] == saved["id"]), "gross_pay": 2500001, "net_pay": 2232351}
    svc._write_hr_record("payroll_statements", drifted, ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(saved["id"], ADMIN)
    assert exc.value.status_code == 409 and "지급 항목 합계" in exc.value.detail
    assert svc._read_hr("payroll_statements", ADMIN)[0]["status"] == "draft"
    with pytest.raises(pdf.PayrollPdfError):
        pdf.render_payroll_statement_pdf({**drifted, "status": "confirmed"})


def test_item_change_on_confirmed_statement_creates_new_version_not_overwrite():
    first = confirmed(wage_payload())
    moved = [{**PAY_ITEMS[0], "amount": 1800000}, {**PAY_ITEMS[1], "amount": 400000}, *PAY_ITEMS[2:]]
    second = svc.save_payroll({**wage_payload(pay_items=moved), "id": first["id"]}, ADMIN)
    assert second["id"] != first["id"] and second["version"] == 2 and second["status"] == "draft"
    stored = {row["id"]: row for row in svc._read_hr("payroll_statements", ADMIN)}
    assert stored[first["id"]]["status"] == "confirmed"
    assert stored[first["id"]]["pay_items"][0]["amount"] == 1900000


def test_same_content_resave_does_not_create_version():
    first = confirmed(wage_payload())
    again = svc.save_payroll({**wage_payload(), "id": first["id"]}, ADMIN)
    assert again["id"] == first["id"]
    assert len(svc._read_hr("payroll_statements", ADMIN)) == 1


# --- 과거 명세 호환 -------------------------------------------------------------------------


def test_legacy_statement_without_items_still_reads_confirms_and_renders():
    legacy = {
        "employee_email": EMPLOYEE_EMAIL, "employee_name": "가입 직원", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "payroll_month": MONTH, "gross_pay": 3000000, "tax_withholding": 100000, "insurance_deduction": 200000, "other_deduction": 0,
    }
    saved = confirmed_import(legacy)
    assert "pay_items" not in saved and "deduction_items" not in saved and "work_summary" not in saved
    totals = pdf.payroll_totals(saved)
    assert totals["consistent"] and totals["net_pay"] == 2700000 and totals["item_errors"] == []
    rows = pdf.payroll_pdf_rows(saved)
    assert row_value(rows, "원천세") == "100,000원" and row_value(rows, "4대보험 공제") == "200,000원"
    assert row_value(rows, "실지급액") == "2,700,000원"
    assert row_value(rows, "근로일수·근로시간") == "기재 없음"
    data, _ = svc.payroll_pdf_for_download(saved["id"], ADMIN)
    text = pdf_text(data)
    assert "임금명세서" in text and "2,700,000원" in text
    assert row_value(pdf.payroll_pdf_rows(svc._payroll_for_pdf(saved, ADMIN)), "사원번호") in {"E-0007", "join-mia"}


# --- 프리랜서 3.3% / 용역비 지급명세서 ------------------------------------------------------------


@pytest.mark.parametrize("gross,income,local,net", [
    (1000000, 30000, 3000, 967000),
    (1234567, 37030, 3700, 1193837),
    (0, 0, 0, 0),
    (333, 0, 0, 333),
    (340, 10, 0, 330),
])
def test_freelancer_withholding_rule(gross, income, local, net):
    tax = pdf.freelancer_withholding(gross)
    assert (tax["income_tax"], tax["local_income_tax"], tax["total"]) == (income, local, income + local)
    assert gross - tax["total"] == net


def test_freelancer_rows_have_no_wage_or_insurance_words_and_split_tax():
    saved = confirmed(freelancer_payload(1000000, source_contract_id="ignored"))
    assert pdf.payroll_form(saved) == "freelancer"
    rows = pdf.payroll_pdf_rows(saved)
    assert rows == pdf.freelancer_statement_rows(saved)
    flat = " ".join(f"{a} {b}" for a, b in rows)
    assert "급여" not in flat and "4대보험" not in flat
    assert row_value(rows, "지급 용역비") == "1,000,000원"
    assert row_value(rows, "사업소득세(3%)") == "30,000원"
    assert row_value(rows, "지방소득세(0.3%)") == "3,000원"
    assert row_value(rows, "실지급액") == "967,000원"
    for label in ("용역계약 ID", "용역기간", "업무범위"):
        assert label in labels(rows)


def test_freelancer_pdf_uses_service_fee_statement_form():
    saved = confirmed(freelancer_payload(1234567, settlement_basis={"scope": "메뉴 사진 촬영"}))
    data = pdf.render_payroll_statement_pdf({**saved, "settlement_basis": {
        "contract_id": "c-1", "period_start": "2026-10-01", "period_end": "2026-10-31", "scope": "메뉴 사진 촬영"}})
    text = pdf_text(data)
    assert "용역비 지급명세서" in text and "임금명세서" not in text
    assert "급여" not in text and "4대보험" not in text
    for needle in ("37,030원", "3,700원", "1,193,837원", "메뉴 사진 촬영", "2026-10-01 ~ 2026-10-31", "c-1"):
        assert needle in text


def test_freelancer_legacy_total_without_items_does_not_invent_split():
    legacy = {
        "employee_name": "가입 직원", "payroll_month": MONTH, "employment_tax_type": "freelancer_33", "status": "confirmed",
        "gross_pay": 1234567, "tax_withholding": 40740, "insurance_deduction": 0, "other_deduction": 0, "net_pay": 1193827,
    }
    rows = pdf.payroll_pdf_rows(legacy)
    assert "사업소득세(3%)" not in labels(rows)
    assert row_value(rows, "원천징수세액(소득세+지방소득세)") == "40,740원"
    matching = {**legacy, "tax_withholding": 40730, "net_pay": 1193837}
    assert row_value(pdf.payroll_pdf_rows(matching), "사업소득세(3%)") == "37,030원"


def test_freelancer_rejects_insurance_deduction():
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(freelancer_payload(1000000, insurance_deduction=1000, deduction_items=None), ADMIN)
    assert exc.value.status_code == 400 and "4대보험" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(freelancer_payload(1000000, deduction_items=[
            {"code": "national_pension", "amount": 1000}, {"code": "income_tax", "amount": 29000},
            {"code": "local_income_tax", "amount": 3000}], tax_withholding=32000), ADMIN)
    assert exc.value.status_code == 400


# --- 관리자 화면(payroll-admin.js) ------------------------------------------------------

PAYROLL_ADMIN_JS = Path(__file__).resolve().parents[2] / "app/static/apps/obys/modules/payroll-admin.js"

_ADMIN_HARNESS = r"""
const assert=require('node:assert/strict');global.window={};global.confirm=()=>true;
require(process.argv[1]);
const amounts=['gross_pay','taxable_pay','non_tax_meal_allowance','tax_withholding','insurance_deduction','other_deduction'];
const controls=new Map();
const element=key=>{if(!controls.has(key))controls.set(key,{value:'',textContent:'',innerHTML:'',style:{},disabled:false});return controls.get(key)};
amounts.forEach(key=>{const control=element(key);let value='';Object.defineProperty(control,'value',{get:()=>value,set:next=>{value=String(next)}})});
const form=element('form');form.elements=Object.fromEntries(amounts.map(k=>[k,element(k)]));form.reportValidity=()=>true;
const actions=['defaults','draft','settlement','confirm','pdf','versions','pay','deliver'];
actions.forEach(action=>element(`[data-payroll-action="${action}"]`).dataset={payrollAction:action});
const host={isConnected:true,innerHTML:'',querySelector:element,
  querySelectorAll:selector=>selector==='[data-payroll-action]'?actions.map(a=>element(`[data-payroll-action="${a}"]`)):[...controls.values()].filter(c=>c!==element('[data-payroll-items]'))};
let saved=null;
let row=Object.assign({id:'s1',business_id:'b1',employee_email:'one@example.test',payroll_month:'2026-11',status:'draft',version:1,
  employment_tax_type:'freelancer_33',gross_pay:1000000,taxable_pay:1000000,non_tax_meal_allowance:0,tax_withholding:0,
  insurance_deduction:0,other_deduction:50000,net_pay:950000,totals:{total_deductions:50000}}, ROW_PATCH);
const api=async(path,options={})=>{
  if(path.startsWith('/employees/approved'))return {employees:[{id:'e1',email:'one@example.test',name:'합성 직원',branch:'A'}]};
  if(path.startsWith('/payroll?'))return {statements:[row]};
  if(path==='/payroll'){saved=JSON.parse(options.body);row=Object.assign({},row,saved,{net_pay:0});return {statement:row}}
  throw Error('unexpected API '+path);
};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const itemsHost=element('[data-payroll-items]');
const press=async dataset=>{itemsHost.onclick({target:{closest:()=>({dataset})}});await settle()};
(async()=>{
  await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11',canManage:true});
  BODY
})().catch(e=>{console.error(e);process.exitCode=1});
"""


def _run_admin_js(body, row_patch="{}"):
    script = _ADMIN_HARNESS.replace("BODY", body).replace("ROW_PATCH", row_patch)
    result = subprocess.run(["node", "-e", script, str(PAYROLL_ADMIN_JS)], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node 가 없는 환경 — 호스트에서 실행")


@needs_node
def test_admin_ui_auto_33_keeps_legacy_other_deduction():
    _run_admin_js("""
      assert.equal(form.elements.other_deduction.value,'50000');
      await press({itemTax:'1'});
      assert.equal(form.elements.tax_withholding.value,'33000');
      assert.equal(form.elements.other_deduction.value,'50000');
      form.onsubmit({preventDefault(){}});await settle();
      const other=saved.deduction_items.filter(r=>r.code==='other');
      assert.equal(other.length,1);assert.equal(other[0].amount,50000);
      assert.equal(saved.deduction_items.reduce((t,r)=>t+r.amount,0),83000);
    """)


@needs_node
def test_admin_ui_refuses_to_drop_legacy_insurance_when_switching_to_items():
    _run_admin_js("""
      form.elements.insurance_deduction.value='10000';
      await press({itemAdd:'ded'});
      assert.equal(form.elements.insurance_deduction.value,'10000');
      assert.match(element('[data-payroll-message]').textContent,/보험 공제/);
    """, "{employment_tax_type:'four_insurance',insurance_deduction:10000,other_deduction:0}")


@needs_node
def test_admin_ui_first_pay_item_keeps_simple_input_gross():
    _run_admin_js("""
      form.elements.gross_pay.value='1000000';form.elements.non_tax_meal_allowance.value='200000';form.elements.taxable_pay.value='800000';
      await press({itemAdd:'pay'});
      assert.equal(form.elements.gross_pay.value,'1000000');
      assert.equal(form.elements.taxable_pay.value,'800000');
      itemsHost.oninput({target:{dataset:{item:'pay',i:'2',k:'amount'},value:'100000'}});
      assert.equal(form.elements.gross_pay.value,'1100000');
      assert.equal(form.elements.taxable_pay.value,'900000');
      form.onsubmit({preventDefault(){}});await settle();
      assert.deepEqual(saved.pay_items.map(r=>[r.code,r.amount]),[['base',800000],['meal',200000],['overtime',100000]]);
      assert.equal(saved.gross_pay,1100000);
    """, "{employment_tax_type:'four_insurance',gross_pay:0,taxable_pay:0,other_deduction:0}")


@needs_node
def test_admin_ui_first_pay_item_with_empty_gross_adds_blank_row_only():
    _run_admin_js("""
      form.elements.gross_pay.value='';
      await press({itemAdd:'pay'});
      assert.equal(form.elements.gross_pay.value,'0');
    """, "{employment_tax_type:'four_insurance',gross_pay:0,taxable_pay:0,other_deduction:0}")


@needs_node
def test_admin_ui_splits_legacy_withholding_into_income_and_local_tax():
    _run_admin_js("""
      await press({itemAdd:'ded'});
      await press({itemDel:'ded',i:'2'});
      form.onsubmit({preventDefault(){}});await settle();
      assert.deepEqual(saved.deduction_items.map(r=>[r.code,r.amount]),[['income_tax',30000],['local_income_tax',3000]]);
      assert.equal(form.elements.tax_withholding.value,'33000');
    """, "{tax_withholding:33000,other_deduction:0,net_pay:967000}")


@needs_node
def test_admin_ui_refuses_to_guess_when_withholding_does_not_split_exactly():
    _run_admin_js("""
      await press({itemAdd:'ded'});
      assert.equal(form.elements.tax_withholding.value,'33005');
      assert.match(element('[data-payroll-message]').textContent,/소득세와 지방소득세로 나눌 수 없습니다/);
      assert.equal(itemsHost.innerHTML.includes('data-k="amount"'),false);
    """, "{tax_withholding:33005,other_deduction:0}")


@needs_node
def test_admin_ui_sends_pay_date_only_when_entered():
    _run_admin_js("""
      form.onsubmit({preventDefault(){}});await settle();
      assert.equal('pay_date' in saved,false);
      element('[data-payroll-pay-date]').value=' 매월 10일 ';
      form.onsubmit({preventDefault(){}});await settle();
      assert.equal(saved.pay_date,'매월 10일');
    """)


def test_payroll_for_pdf_blank_identity_does_not_override_employee_record(monkeypatch):
    employee = {"id": "emp-7", "employee_no": "A-007", "birth_date": "1990-02-03", "email": EMPLOYEE_EMAIL}
    monkeypatch.setattr(svc, "_find_employee_record", lambda *args, **kwargs: employee)
    row = {"employee_email": EMPLOYEE_EMAIL, "employee_no": "", "employee_birth_date": None, "gross_pay": 1}
    merged = svc._payroll_for_pdf(row, ADMIN)
    assert merged["employee_no"] == "A-007" and merged["employee_birth_date"] == "1990-02-03"
    kept = svc._payroll_for_pdf({**row, "employee_no": "X-1", "employee_birth_date": "1980-01-01"}, ADMIN)
    assert kept["employee_no"] == "X-1" and kept["employee_birth_date"] == "1980-01-01"


# --- 확정 시 필수 기재사항 / 프리랜서 기타 공제 항목별 출력 --------------------------------------------


def _bare_wage(**overrides):
    return wage_payload(pay_items=None, deduction_items=None, work_summary=None, **overrides)


def test_new_wage_statement_without_pay_items_cannot_be_confirmed_but_draft_is_allowed():
    draft = svc.save_payroll(_bare_wage(), ADMIN)
    assert draft["status"] == "draft"
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409 and "임금명세서 필수 기재사항(구성항목별 금액)이 비어 있습니다" in exc.value.detail
    with pytest.raises(HTTPException) as exc2:
        svc.save_payroll({**_bare_wage(), "status": "confirmed"}, ADMIN)
    assert exc2.value.status_code == 409
    assert {row["status"] for row in svc._read_hr("payroll_statements", ADMIN)} == {"draft"}


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_new_wage_statement_without_pay_date_cannot_be_confirmed(blank):
    draft = svc.save_payroll(wage_payload(pay_date=blank), ADMIN)
    assert pdf.wage_statement_required_errors(draft) == ["임금명세서 필수 기재사항(임금지급일)이 비어 있습니다"]
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409 and "임금지급일" in exc.value.detail
    filled = svc.save_payroll({**wage_payload(), "id": draft["id"], "pay_date": "2026-11-10"}, ADMIN)
    assert pdf.wage_statement_required_errors(filled) == []
    assert svc.confirm_payroll(filled["id"], ADMIN)["status"] == "confirmed"


def test_wage_statement_with_deduction_but_no_deduction_items_is_blocked():
    payload = wage_payload(deduction_items=None)
    draft = svc.save_payroll(payload, ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409 and "공제 항목별 금액" in exc.value.detail


def test_overtime_night_holiday_time_requires_calc_note_on_matching_item():
    no_note = [{**item, "calc_note": ""} if item["code"] == "overtime" else item for item in PAY_ITEMS]
    draft = svc.save_payroll(wage_payload(pay_items=no_note), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409 and "연장근로수당 계산방법" in exc.value.detail
    night = svc.save_payroll(wage_payload(work_summary={**WORK_SUMMARY, "night_minutes": 60}, employee_email=EMPLOYEE_EMAIL, memo="night"), ADMIN)
    with pytest.raises(HTTPException) as exc2:
        svc.confirm_payroll(night["id"], ADMIN)
    assert "야간근로수당 계산방법" in exc2.value.detail


def test_wage_statement_with_all_required_items_confirms():
    saved = confirmed(wage_payload())
    assert saved["status"] == "confirmed"
    assert pdf.wage_statement_required_errors(saved) == []


def test_zero_deduction_wage_statement_needs_no_deduction_items():
    pay = [{"code": "base", "label": "기본급", "amount": 1000000, "calc_note": "월급"}]
    saved = confirmed(wage_payload(
        gross_pay=1000000, taxable_pay=1000000, non_tax_meal_allowance=0, tax_withholding=0, insurance_deduction=0,
        pay_items=pay, deduction_items=None, work_summary=None, meal_provision="",
    ))
    assert saved["status"] == "confirmed"


def test_client_cannot_mark_statement_as_import_to_skip_required_items():
    draft = svc.save_payroll(_bare_wage(source_kind="legacy_import", import_task="forged"), ADMIN)
    assert "source_kind" not in draft and "import_task" not in draft
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409


def test_nested_statement_payload_marker_cannot_skip_required_items():
    forged = {"source_kind": "legacy_import", "import_task": "forged"}
    draft = svc.save_payroll({**_bare_wage(), "statement_payload": forged}, ADMIN)
    assert "statement_payload" not in draft
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409
    assert pdf.wage_statement_required_errors({**_bare_wage(), "statement_payload": forged})


def test_overtime_amount_without_work_summary_still_requires_calc_note():
    pay = [
        {"code": "base", "label": "기본급", "amount": 1500000, "calc_note": "월급"},
        {"code": "overtime", "label": "연장근로수당", "amount": 1000000, "calc_note": ""},
    ]
    payload = wage_payload(
        gross_pay=2500000, taxable_pay=2500000, non_tax_meal_allowance=0, meal_provision="",
        pay_items=pay, work_summary=None,
    )
    assert any("연장근로수당 계산방법" in e for e in pdf.wage_statement_required_errors(payload))
    draft = svc.save_payroll(payload, ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409 and "연장근로수당 계산방법" in exc.value.detail


def test_every_item_of_same_code_needs_calc_note():
    pay = [
        {"code": "overtime", "label": "연장근로수당 A", "amount": 100000, "calc_note": "연장 10시간 × 1.5"},
        {"code": "overtime", "label": "연장근로수당 B", "amount": 100000, "calc_note": ""},
    ]
    errors = pdf.wage_statement_required_errors({"gross_pay": 200000, "pay_items": pay, "work_summary": None})
    assert any("연장근로수당 계산방법" in e for e in errors)


@pytest.mark.parametrize("marker", [{"source_kind": "legacy_import"}, {"source_kind": "bulk_import"}, {"import_task": "task-1"}])
def test_import_wage_statement_without_items_confirms_and_renders_pdf(marker):
    saved = confirmed_import(_bare_wage(), **marker)
    assert saved["status"] == "confirmed" and "pay_items" not in saved
    data, _ = svc.payroll_pdf_for_download(saved["id"], ADMIN)
    assert "임금명세서" in pdf_text(data) and "2,500,000원" in pdf_text(data)


def test_already_confirmed_statement_without_items_is_still_readable_and_renders():
    legacy = _bare_wage()
    draft = svc.save_payroll(legacy, ADMIN)
    stored = next(row for row in svc._read_hr("payroll_statements", ADMIN) if row["id"] == draft["id"])
    svc._write_hr_record("payroll_statements", {**stored, "status": "confirmed", "confirmed_by": ADMIN["email"]}, ADMIN)
    data, _ = svc.payroll_pdf_for_download(draft["id"], ADMIN)
    assert "임금명세서" in pdf_text(data)


def test_freelancer_confirm_is_not_subject_to_wage_required_items():
    saved = confirmed(freelancer_payload(1000000, deduction_items=None))
    assert saved["status"] == "confirmed"


def test_freelancer_pdf_lists_each_other_deduction_item():
    gross = 1000000
    tax = pdf.freelancer_withholding(gross)
    saved = confirmed(freelancer_payload(
        gross, other_deduction=30000,
        deduction_items=[
            {"code": "income_tax", "label": "소득세", "amount": tax["income_tax"]},
            {"code": "local_income_tax", "label": "지방소득세", "amount": tax["local_income_tax"]},
            {"code": "other", "label": "장비 대여료", "amount": 10000},
            {"code": "other", "label": "선지급 회수", "amount": 20000},
        ],
    ))
    rows = pdf.payroll_pdf_rows(saved)
    assert row_value(rows, "장비 대여료") == "10,000원" and row_value(rows, "선지급 회수") == "20,000원"
    assert "기타 공제" not in labels(rows)
    assert row_value(rows, "공제 합계") == "63,000원" and row_value(rows, "실지급액") == "937,000원"
    text = pdf_text(svc.payroll_pdf_for_download(saved["id"], ADMIN)[0])
    assert "장비 대여료" in text and "선지급 회수" in text


def test_freelancer_legacy_other_deduction_without_items_stays_single_line():
    saved = confirmed(freelancer_payload(1000000, other_deduction=5000, deduction_items=None))
    assert row_value(pdf.payroll_pdf_rows(saved), "기타 공제") == "5,000원"

"""지원 근무·대표 인출·가족 급여의 기존 두 화면 표시 회귀 검증."""
from tests.unit.test_obys_payroll_admin_summary import _run
from tests.unit.test_obys_payroll_deduction_detail import _run as _index_run
from pathlib import Path


def test_admin_summary_excludes_owner_draw_and_exempts_contracts():
    body = '''const rows=[
    {__live:{raw:{gross_pay:200000,tax_withholding:1000,insurance_deduction:2000,other_deduction:0,net_pay:197000,statement_payload:{payee_relation:'owner',owner_withdrawal:true}}}},
    {__live:{raw:{gross_pay:150000,tax_withholding:0,insurance_deduction:1350,other_deduction:0,net_pay:148650,statement_payload:{payee_relation:'family',payee_warnings:['근무 증빙 필요']}}}}
    ]; console.log(JSON.stringify({summary:payrollSummaryV41(rows),flags:rows.map(payrollFlagsV41),html:payrollSummaryHtmlV41(rows,'2026-10')}));'''
    got = _run(body)
    assert got["summary"]["gross"] == 150000
    assert got["summary"]["withdrawals"] == 200000
    assert got["summary"]["deductions"] == 1350
    assert got["summary"]["net"] == 148650
    assert got["summary"]["noContract"] == 0
    assert got["summary"]["contractNone"] == 2
    assert "계약 없음(대표)" in got["flags"][0]
    assert "계약 없음(가족)" in got["flags"][1]
    assert "근무 증빙 필요" in got["flags"][1]
    assert "계약 미연결" not in "".join(got["flags"])


def test_index_totals_separate_owner_withdrawal():
    got = _index_run({"gross_pay":200000,"net_pay":200000,"owner_withdrawal":True})["totals"]
    assert "대표 인출(비용 아님) 400,000원 별도" in got
    assert "0명" in got
    assert "총지급 400,000원" not in got


def test_support_days_and_amount_badge():
    got = _run("""console.log(JSON.stringify(payrollFlagsV41({__live:{raw:{statement_payload:{pay_items:[{code:'support_shift',work_date:'2026-10-01',amount:120000},{code:'support_shift',work_date:'2026-10-01',amount:20000},{code:'support_shift',work_date:'2026-10-02',amount:120000}]}}}})))""")
    assert "지원 2일 · 260,000원" in got


def test_editor_support_and_relation_inputs_are_present():
    src = (Path(__file__).resolve().parents[2] / "app/static/apps/obys/modules/payroll-admin.js").read_text()
    for text in ("지원 근무 추가", 'name="payee_relation"', 'name="relation_note"', 'name="work_memo"', 'support_branch:row.support_branch', 'quantity:Number(row.quantity)', 'font-size:14px', 'min-height:44px'):
        assert text in src

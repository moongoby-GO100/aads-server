"""오비서 관리자 급여 현황 — 월 합계·지점 열/필터·계약 미연결·공제 미분리 표시 검증."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "admin.html"
SRC = HTML.read_text(encoding="utf-8")
NAMES = (
    "payrollPayloadV41", "payrollNumV41", "payrollDeductionTotalV41", "payrollContractIdV41",
    "payrollUnsplitV41", "payrollPaymentCheckV41", "payrollFlagsV41", "payrollSummaryV41", "payrollSummaryHtmlV41", "payrollBasisHtmlV41",
)


def _fn(name: str) -> str:
    start = SRC.index(f"    function {name}(")
    end = SRC.index("\n", start)
    return SRC[start:end]


def _run(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    prelude = "const escapeHtml=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');\n"
    script = prelude + "\n".join(_fn(n) for n in NAMES) + "\n" + body
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


ROWS = """
const mk=(raw)=>({__live:{raw}});
const rows=[
  mk({status:'confirmed',gross_pay:3200000,tax_withholding:81780,insurance_deduction:291520,other_deduction:0,net_pay:2826700,statement_payload:{source_contract_id:'c-hong'}}),
  mk({status:'draft',gross_pay:3000000,tax_withholding:0,insurance_deduction:0,other_deduction:359690,net_pay:2640310,statement_payload:JSON.stringify({deduction_split_note:'한 칸',source_tab:'2026년09월급여_10월지급',source_row:'6'})}),
  mk({status:'draft',gross_pay:546000,tax_withholding:18010,insurance_deduction:0,other_deduction:0,net_pay:527990,statement_payload:{contract_id:'c-yang'}}),
];
"""


def test_summary_totals_match_rows():
    got = _run(ROWS + "console.log(JSON.stringify(payrollSummaryV41(rows)))")
    assert got == {"count": 3, "gross": 6746000, "deductions": 751000, "net": 5995000,
                   "confirmed": 1, "pending": 2, "noContract": 1, "unsplit": 1}


def test_flags_mark_missing_contract_and_unsplit_deduction():
    got = _run(ROWS + "console.log(JSON.stringify(rows.map(payrollFlagsV41)))")
    assert got[0] == ""
    assert "계약 미연결" in got[1] and "공제 미분리" in got[1]
    assert got[2] == ""


def test_payload_string_or_broken_json_is_safe():
    got = _run("console.log(JSON.stringify([payrollPayloadV41({__live:{raw:{statement_payload:'{bad'}}}),payrollPayloadV41({}),payrollPayloadV41({__live:{raw:{statement_payload:'{\"contract_id\":\"x\"}'}}})]))")
    assert got == [{}, {}, {"contract_id": "x"}]


def test_basis_explains_unsplit_and_source():
    html = _run(ROWS + "console.log(JSON.stringify(payrollBasisHtmlV41(rows[1])))")
    assert "공제 미분리" in html and "계약 미연결" in html and "2026년09월급여_10월지급 6행" in html
    html = _run(ROWS + "console.log(JSON.stringify(payrollBasisHtmlV41({__live:{raw:{statement_payload:{contract_id:'abcdef123456',deduction_items:[{code:'national_pension',label:'국민연금',amount:142500}]}}}})))")
    assert "국민연금 142,500원" in html and "연결 계약</b> abcdef12" in html


def test_summary_html_escapes_and_marks_pending():
    html = _run(ROWS + "console.log(JSON.stringify(payrollSummaryHtmlV41(rows,'2026-10')))")
    assert 'id="payrollSummary"' in html and "6,746,000원" in html and "확정 대기" in html and "2건" in html


def test_render_wires_summary_branch_column_and_filter():
    assert 'body=payrollSummaryHtmlV41(inMonth,month)+section("직원별 급여"' in SRC
    assert 'table(inMonth,["직원","지점","총지급액","공제","실수령","상태"]' in SRC
    assert 'payrollFlagsV41(row)+payrollRowNoteV41(row):""' in SRC
    assert 'id="payrollBranch"' in SRC and 'getElementById("payrollBranch")?.addEventListener("change"' in SRC
    assert '["급여번호","직원","지점","총지급액"' in SRC
    assert '"지점":String(raw.branch||"").trim()||"지점 미등록"' in SRC


PAY = """
const mk=(raw)=>({__live:{raw}});
const sheetOnly=mk({status:'draft',net_pay:2746320,statement_payload:{contract_id:'c',raw_net:2665440,deduction_items:[{code:'national_pension',amount:137750}]}});
const same=mk({status:'draft',net_pay:2826700,statement_payload:{contract_id:'c',raw_net:2826700,deduction_items:[{code:'national_pension',amount:1}]}});
const bank=mk({status:'draft',net_pay:2746320,statement_payload:{contract_id:'c',raw_net:2665440,bank_paid_amount:2700000,deduction_items:[{code:'national_pension',amount:1}]}});
const bankSame=mk({status:'draft',net_pay:2746320,statement_payload:{contract_id:'c',raw_net:2665440,bank_paid_amount:2746320,deduction_items:[{code:'national_pension',amount:1}]}});
const none=mk({status:'draft',net_pay:100,statement_payload:{contract_id:'c'}});
"""


def test_payment_check_prefers_bank_then_sheet():
    got = _run(PAY + "console.log(JSON.stringify([sheetOnly,same,bank,bankSame,none].map(payrollPaymentCheckV41)))")
    assert got[0] == {"net": 2746320, "sheet": 2665440, "bank": None, "sheetDiff": 80880, "bankDiff": None}
    assert got[1]["sheetDiff"] == 0
    assert got[2]["bankDiff"] == 46320
    assert got[3]["bankDiff"] == 0
    assert got[4] == {"net": 100, "sheet": None, "bank": None, "sheetDiff": None, "bankDiff": None}


def test_flags_show_payment_difference_only_when_it_exists():
    got = _run(PAY + "console.log(JSON.stringify([sheetOnly,same,bank,bankSame,none].map(payrollFlagsV41)))")
    assert "시트 지급 차액 +80,880원" in got[0] and 'data-flag="bank-diff"' not in got[0]
    assert got[1] == ""
    assert "계좌 지급 차액 +46,320원" in got[2] and "시트 지급" not in got[2]
    assert got[3] == ""  # 계좌 금액이 일치하면 시트 차이는 표시하지 않는다
    assert got[4] == ""


def test_basis_explains_bank_reconciliation_state():
    html = _run(PAY + "console.log(JSON.stringify([payrollBasisHtmlV41(sheetOnly),payrollBasisHtmlV41(bank)]))")
    assert "계좌 출금 내역이 아직 수집되지 않아" in html[0] and "2,665,440원 대비 차액 +80,880원" in html[0]
    assert "계좌 출금 2,700,000원 · 차액 +46,320원" in html[1]

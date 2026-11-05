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
    "payrollUnsplitV41", "payrollPaymentCheckV41", "payrollFlagsV41", "payrollPayoutV41", "payrollPayoutBadgeV41", "payrollPayoutOpenCountV41", "payrollPayoutPanelV41", "payrollSummaryV41", "payrollSummaryHtmlV41", "payrollBasisHtmlV41",
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


PAYOUT = """
const mk=(raw)=>({__live:{id:raw.id,raw}});
const under=mk({id:'s1',status:'paid',net_pay:2700000,statement_payload:{contract_id:'c',payout:{amount:2619120}}});
const over=mk({id:'s2',status:'confirmed',net_pay:2700000,statement_payload:{contract_id:'c',payout:{amount:2800000}}});
const sheetConfirmed=mk({id:'s3',status:'confirmed',net_pay:2746320,statement_payload:{contract_id:'c',raw_net:2665440}});
const planned=mk({id:'s4',status:'paid',net_pay:2700000,statement_payload:{contract_id:'c',payout:{amount:2619120},payout_diff_resolution:{method:'next_month_pay',diff_amount:-80880}}});
const extraDone=mk({id:'s5',status:'paid',net_pay:2700000,statement_payload:{contract_id:'c',payout:{amount:2619120},payout_diff_resolution:{method:'extra_pay',diff_amount:-80880,completed_at:'x',completed_date:'2026-11-12'}}});
const draftDiff=mk({id:'s6',status:'draft',net_pay:2746320,statement_payload:{contract_id:'c',raw_net:2665440}});
"""


def test_payout_badge_open_and_done_states():
    got = _run(PAYOUT + "console.log(JSON.stringify([under,over,sheetConfirmed,planned,extraDone,draftDiff].map(payrollFlagsV41)))")
    assert "덜 지급 80,880원 · 처리 필요" in got[0] and 'class="badge warn"' in got[0]
    assert "더 지급 100,000원 · 처리 필요" in got[1]
    assert "덜 지급 80,880원 · 처리 필요" in got[2] and "시트 지급 차액" not in got[2]
    assert "다음 달 정산 예정" in got[3] and "처리 필요" not in got[3] and "badge warn" not in got[3]
    assert "추가 지급 완료" in got[4]
    assert "시트 지급 차액 +80,880원" in got[5] and "처리 필요" not in got[5]  # 초안은 기존 표시 유지


def test_payout_priority_prefers_payout_over_bank_and_sheet():
    got = _run("console.log(JSON.stringify(payrollPayoutV41({__live:{raw:{status:'paid',net_pay:1000,statement_payload:{raw_net:900,bank_paid_amount:950,payout:{amount:800}}}}})))")
    assert got["basis"] == "payout" and got["diff"] == -200


def test_summary_note_counts_open_payout_diffs():
    html = _run(PAYOUT + "console.log(JSON.stringify(payrollSummaryHtmlV41([under,over,planned,draftDiff],'2026-10')))")
    assert "차액 미처리 2건" in html and "확정 대기" in html
    html = _run(PAYOUT + "console.log(JSON.stringify(payrollSummaryHtmlV41([planned],'2026-10')))")
    assert "차액 미처리 0건" in html


def test_payout_panel_offers_only_sign_matching_choices():
    under_html, over_html, draft_html, done_html, ok_html = _run(PAYOUT + "console.log(JSON.stringify([under,over,draftDiff,planned,mk({id:'z',status:'paid',net_pay:5,statement_payload:{payout:{amount:5}}})].map(payrollPayoutPanelV41)))")
    assert 'value="extra_pay"' in under_html and 'value="next_month_pay"' in under_html
    assert "next_month_recover" not in under_html and "direct_refund" not in under_html and "match_paid" not in under_html
    assert 'value="next_month_recover"' in over_html and 'value="direct_refund"' in over_html and "extra_pay" not in over_html
    assert "기준명세를 먼저 확정하세요" in draft_html and "<input" not in draft_html
    assert 'data-payout-diff="cancel"' in done_html and 'data-payout-diff="save"' not in done_html
    assert ok_html == ""
    assert "min-height:44px" in under_html and 'data-payout-diff="save"' in under_html


SETTLED = """
const settled=mk({id:'t1',status:'paid',net_pay:2700000,pending_amount:0,settled_amount:-80880,settled_in:[{statement_id:'n',payroll_month:'2026-11'}],statement_payload:{contract_id:'c',payout:{amount:2619120},payout_diff_resolution:{method:'next_month_pay',diff_amount:-80880}}});
const remainder=mk({id:'t2',status:'paid',net_pay:2710000,pending_amount:-10000,settled_amount:-80880,settled_in:[{statement_id:'n',payroll_month:'2026-11'}],statement_payload:{contract_id:'c',payout:{amount:2619120},payout_diff_resolution:{method:'next_month_pay',diff_amount:-80880}}});
const completed=mk({id:'t4',status:'paid',net_pay:2700000,pending_amount:0,settled_amount:0,settled_in:[],statement_payload:{contract_id:'c',payout:{amount:2619120},bank_paid_amount:2619120,payout_diff_resolution:{method:'extra_pay',diff_amount:-80880,completed_at:'x',completed_date:'2026-11-12'}}});
const flipped=mk({id:'t3',status:'paid',net_pay:2670000,pending_amount:30000,settled_amount:-80880,resolution_invalid:true,settled_in:[{statement_id:'n',payroll_month:'2026-11'}],statement_payload:{contract_id:'c',payout:{amount:2619120},payout_diff_resolution:{method:'next_month_pay',diff_amount:-80880}}});
"""


def test_badge_and_panel_use_computed_ledger_values():
    badges = _run(PAYOUT + SETTLED + "console.log(JSON.stringify([settled,remainder,flipped,completed].map(payrollFlagsV41)))")
    assert "추가 지급 완료" in badges[3] and "계좌 지급 차액" not in badges[3]
    assert "다음 달 정산 반영됨" in badges[0] and "처리 필요" not in badges[0]
    assert "다음 달 정산 예정" in badges[1]
    assert "더 지급 30,000원 · 정산 후 차액 재발생 · 처리 필요" in badges[2]
    settled, remainder, flipped = _run(PAYOUT + SETTLED + "console.log(JSON.stringify([settled,remainder,flipped].map(payrollPayoutPanelV41)))")
    assert "남은 차액 없음" in settled and "2026-11 명세" in settled and 'data-payout-diff="cancel"' not in settled
    assert "덜 지급 10,000원" in remainder and "정산 반영 80,880원" in remainder and 'data-payout-diff="save"' not in remainder
    assert "다시 선택하세요" in flipped and 'value="next_month_recover"' in flipped and 'value="direct_refund"' in flipped
    assert 'value="next_month_pay"' not in flipped and 'data-payout-diff="save"' in flipped
    html = _run(PAYOUT + SETTLED + "console.log(JSON.stringify(payrollSummaryHtmlV41([settled,remainder,flipped],'2026-10')))")
    assert "차액 미처리 1건" in html


def test_draft_claim_is_shown_apart_from_settled_and_blocks_cancel():
    claim = """const claimed=mk({id:'t5',status:'paid',net_pay:2700000,pending_amount:-80880,settled_amount:0,claimed_amount:-80880,settled_in:[],claimed_in:[{statement_id:'n',payroll_month:'2026-11'}],statement_payload:{contract_id:'c',payout:{amount:2619120},payout_diff_resolution:{method:'next_month_pay',diff_amount:-80880}}});"""
    badge, panel = _run(PAYOUT + claim + "console.log(JSON.stringify([payrollFlagsV41(claimed),payrollPayoutPanelV41(claimed)]))")
    assert "다음 달 정산 예정" in badge and "처리 필요" not in badge
    assert "초안 청구 80,880원(2026-11 명세)" in panel and "정산 반영" not in panel
    assert 'data-payout-diff="cancel"' not in panel


def test_no_saved_link_is_read_by_screen():
    assert "settled_statement_id" not in SRC
    assert "settlement_of_statement_id" not in (HTML.parent / "modules" / "payroll-admin.js").read_text(encoding="utf-8")


def test_settlement_row_is_read_only_with_exclude_action():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    script = (
        "global.window={};const m=require(process.argv[1]);"
        "console.log(JSON.stringify(m.settlementRowHtml('pay',{label:'전월 미지급분 지급',amount:80880,calc_note:'2026-10 분',settlement_of_root_id:'r1'})))"
    )
    module = HTML.parent / "modules" / "payroll-admin.js"
    out = subprocess.run([node, "-e", script, str(module)], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    html = json.loads(out.stdout)
    assert 'data-settle-off="r1"' in html and "이번 달 제외" in html and "80,880" in html
    script = script.replace("settlement_of_root_id:'r1'})", "settlement_of_root_id:'r1'},true)")
    out = subprocess.run([node, "-e", script, str(module)], capture_output=True, text=True, timeout=20)
    fixed = json.loads(out.stdout)
    assert "data-settle-off" not in fixed and "지급 완료 사실" in fixed and "80,880" in fixed
    assert "<input" not in html and "<select" not in html and "min-height:44px" in html


def test_pay_dialog_defaults_and_parsing():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    script = (
        "global.window={};const m=require(process.argv[1]);"
        "console.log(JSON.stringify([m.payoutDialogHtml({net_pay:2700000},'2026-11-06'),"
        "m.parsePayoutInput({amount:'2,619,120',date:'2026-11-06',source:'bank'}),"
        "m.parsePayoutInput({amount:'-1',date:'2026-11-06'}),m.parsePayoutInput({amount:'10',date:''})]))"
    )
    module = HTML.parent / "modules" / "payroll-admin.js"
    out = subprocess.run([node, "-e", script, str(module)], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    html, ok, bad_amount, bad_date = json.loads(out.stdout)
    assert 'value="2700000"' in html and 'value="2026-11-06"' in html and "min-height:44px" in html
    inherited = subprocess.run([node, "-e", "global.window={};const m=require(process.argv[1]);console.log(m.payoutDialogHtml({net_pay:2700000,payout:{amount:2619120}},'2026-11-06'))", str(module)], capture_output=True, text=True, timeout=20)
    assert 'value="2619120"' in inherited.stdout
    assert ok == {"body": {"paid_amount": 2619120, "paid_date": "2026-11-06", "paid_source": "bank"}}
    assert "error" in bad_amount and "error" in bad_date

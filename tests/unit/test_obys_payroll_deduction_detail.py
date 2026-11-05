"""오비서 급여 화면 — 공제 항목별 상세와 직원 전체 합계 행(표시 전용) 검증."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "index.html"
SRC = HTML.read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SRC.index(f"    function {name}(")
    end = SRC.index("\n    }\n", start) + len("\n    }\n")
    return SRC[start:end]


def test_detail_uses_breakdown_and_tax_keys():
    body = _function("payrollDeductionDetailHtml")
    for key in ("national_pension", "health", "long_term_care", "employment", "income_tax", "local_income_tax", "insurance_breakdown"):
        assert key in body, key
    assert "⚠ 합계 불일치" in body
    assert "세부 미저장" in body


def test_row_template_uses_detail_function():
    assert "${payrollDeductionDetailHtml(item)}" in SRC


def test_totals_row_manager_only():
    assert 'id="payrollTotals"' in SRC
    assert re.search(r"managerMode && !employeeMode && rows\.length \? payrollTotalsRowHtml\(rows\)", SRC)


NODE_SCRIPT = r"""
const formatMoney = v => `${Math.round(Number(v) || 0).toLocaleString("ko-KR")}원`;
%s
%s
%s
const item = JSON.parse(process.argv[1]);
console.log(JSON.stringify({detail: payrollDeductionDetailHtml(item), totals: payrollTotalsRowHtml([item, item])}));
"""

HONG = {
    "employment_tax_type": "four_insurance",
    "gross_pay": 1800000, "taxable_pay": 1800000, "non_tax_meal_allowance": 0,
    "insurance_deduction": 291520, "tax_withholding": 81780, "other_deduction": 0, "net_pay": 1426700,
    "insurance_breakdown": {"national_pension": 142500, "health": 107850, "long_term_care": 14170, "employment": 27000},
    "income_tax": 74350, "local_income_tax": 7430,
}


def _run(item):
    if not shutil.which("node"):
        pytest.skip("node 없음")
    script = NODE_SCRIPT % (_function("payrollAmount"), _function("payrollDeductionDetailHtml"), _function("payrollTotalsRowHtml"))
    out = subprocess.run(["node", "-e", script, json.dumps(item)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_node_render_hong_example():
    detail = _run(HONG)["detail"]
    for text in ("국민연금: 142,500원", "건강보험: 107,850원", "장기요양보험: 14,170원", "고용보험: 27,000원",
                 "4대보험 소계: 291,520원", "소득세: 74,350원", "지방소득세: 7,430원", "세금 소계: 81,780원"):
        assert text in detail, text
    assert "불일치" not in detail


def test_node_mismatch_badge():
    bad = {**HONG, "insurance_deduction": 291521}
    detail = _run(bad)["detail"]
    assert "4대보험 소계: 291,521원 <span class=\"badge warn\">⚠ 합계 불일치" in detail


def test_node_business_income_without_breakdown():
    item = {"employment_tax_type": "freelancer_33", "gross_pay": 1000000, "tax_withholding": 33000, "net_pay": 967000}
    detail = _run(item)["detail"]
    assert "세금 합계: 33,000원" in detail and "세부 미저장" in detail
    assert "국민연금" not in detail and "소득세:" not in detail


def test_node_totals_row():
    totals = _run(HONG)["totals"]
    assert "2명" in totals and "3,600,000원" in totals and "583,040원" in totals and "163,560원" in totals


def _totals(rows):
    if not shutil.which("node"):
        pytest.skip("node 없음")
    script = NODE_SCRIPT.replace("payrollTotalsRowHtml([item, item])", "payrollTotalsRowHtml(JSON.parse(process.argv[2]))") % (
        _function("payrollAmount"), _function("payrollDeductionDetailHtml"), _function("payrollTotalsRowHtml"))
    out = subprocess.run(["node", "-e", script, json.dumps(rows[0]), json.dumps(rows)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)["totals"]


def test_totals_exclude_correction_duplicates():
    orig = {"id": "a", "gross_pay": 1000000, "net_pay": 900000}
    draft = {"id": "b", "supersedes_id": "a", "gross_pay": 1100000, "net_pay": 990000, "status": "draft"}
    old = {"id": "c", "superseded_by": "d", "gross_pay": 500000, "net_pay": 450000}
    totals = _totals([orig, draft, old])
    assert "1명" in totals and "1,000,000원" in totals and "2,100,000원" not in totals


def test_totals_do_not_treat_missing_as_zero():
    totals = _totals([{"id": "a", "gross_pay": None, "net_pay": None}])
    assert "총지급 미집계" in totals and "실지급 미집계" in totals
    mixed = _totals([{"id": "a", "gross_pay": 1000000}, {"id": "b", "gross_pay": None}])
    assert "미집계 1명 제외" in mixed


def test_detail_shows_missing_and_no_detail_notes():
    detail = _run({"employment_tax_type": "four_insurance", "insurance_deduction": 100000})["detail"]
    assert "총지급액: <span class=\"muted\">미집계" in detail and "실지급액: <span class=\"muted\">미집계" in detail
    assert "4대보험 소계: 100,000원 <span class=\"muted\">(세부 미저장)" in detail

"""변경계약서를 열면 원계약의 입사일·종료일이 그대로 들어오고 계약일자는 오늘로 채워지는지 고정한다.

2026-11-03: 변경계약 작성 때 세 날짜를 모두 비워 두어, 김정환(미아점) 변경계약 170bf607 의
입사일 칸에 작성일(2026-11-03)이 들어간 채 서명됐다. 원계약 입사일은 2026-03-30 이다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "app/static/apps/obys/modules/contract-editor-v41.js"
NODE = shutil.which("node")


def _function(src: str, head: str) -> str:
    assert head in src, head
    return head + src.split(head, 1)[1].split("\n  }\n", 1)[0] + "\n  }\n"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_start_amendment_keeps_original_dates_and_fills_contract_date_today() -> None:
    src = EDITOR.read_text(encoding="utf-8")
    body = _function(src, "  function startAmendment() {") + _function(src, "  function suggestionFor(name) {")
    script = """
const snake = s => s.replace(/[A-Z]/g, c => "_" + c.toLowerCase());
const pick = (obj, camel) => { const v = obj?.[snake(camel)] ?? obj?.[camel]; return v === null || v === undefined ? "" : v; };
const original = { id: "f13fab12", status: "signed", start_date: "2026-03-30", end_date: "", contract_date: "2026-03-30",
  employee_request_id: "req-1", business_id: "biz-mia", branch: "열정국밥_미아점", wage: "3000000" };
let E = { contract: original, busy: false };
const S = { employees: [], contracts: [original] };
const C = { today: () => "2026-11-05" };
const statusOf = c => [c.status];
const employeeKey = e => String(e?.id || "");
const freshEditorState = () => ({});
const valuesFromContract = c => ({ wage: String(c.wage), startDate: String(c.start_date), endDate: String(c.end_date),
  contractDate: String(c.contract_date) });
const wageManualFor = () => new Set();
const initSchedule = () => {}; const initAuto = () => {}; const setUrl = () => {}; const render = () => {};
const contractById = id => S.contracts.find(c => String(c.id) === String(id)) || null;
const businessRecord = () => ({}); const businessName = () => "";
%s
startAmendment();
const suggestion = suggestionFor("startDate");
console.log(JSON.stringify({ values: E.values, suggestion }));
""" % body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(out.stdout)
    values = result["values"]
    assert values["amendsContractId"] == "f13fab12"
    assert values["startDate"] == "2026-03-30"
    assert values["contractDate"] == "2026-11-05"
    assert values["wage"] == "3000000"
    assert result["suggestion"] == {"value": "2026-03-30", "note": "원계약 입사일"}

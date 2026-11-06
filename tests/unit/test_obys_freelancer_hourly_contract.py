"""3.3% 프리랜서 용역계약 — 시급제로 작성·저장할 수 있어야 한다.

서버(_validate_contract_payload)는 0dd519f8 부터 freelancer + hourly 를 받는데, 화면 검사
(contract-core.js validateContractDraft, contract-editor-v41.js freelancerConflicts)가 여전히
건별/용역비만 통과시켜 시급 용역계약 등록이 화면에서 막혔다(2026-11-06 정윤선 계약).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULES = REPO / "app" / "static" / "apps" / "obys" / "modules"
CORE = (MODULES / "contract-core.js").read_text(encoding="utf-8")
EDITOR = (MODULES / "contract-editor-v41.js").read_text(encoding="utf-8")

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
  contractDate: "2026-11-02", contractType: "freelancer", employmentTaxType: "freelancer_33",
  startDate: "2026-11-02", workplace: "매장", jobDescription: "매장 지원 용역",
  freelancerScope: "범위", freelancerSettlementTerms: "정산", terminationTerms: "해지", confidentialityTerms: "비밀",
  employerName: "상호", employerRegistrationNo: "000-00-00000", employerRepresentative: "대표", employerAddress: "주소",
  workDays: "월,화,수,목", workTime: "10:00~14:00", weeklyHours: "", wage: 12500,
};
const check = extra => C.checkDraft({ ...base, ...extra });
const auto = A.compute({ contractType: "freelancer", wageType: "hourly", wage: "12500", startDate: "2026-11-02", term: "none" });
console.log(JSON.stringify({
  hourly: check({ wageType: "hourly" }),
  caseFee: check({ wageType: "case_fee" }),
  monthly: check({ wageType: "monthly" }),
  weekly: check({ wageType: "weekly" }),
  autoText: auto.texts.wageComposition || "",
}));
"""


@pytest.fixture(scope="module")
def res() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경")
    out = subprocess.run(
        [node, "-e", NODE_SCRIPT, "--", str(MODULES / "contract-core.js"), str(MODULES / "contract-autofill.js")],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def test_freelancer_hourly_passes_local_check(res):
    assert res["hourly"]["ok"] is True, res["hourly"]


def test_freelancer_case_fee_still_passes(res):
    assert res["caseFee"]["ok"] is True, res["caseFee"]


def test_freelancer_unknown_basis_still_blocked(res):
    # 2026-11-06 B안: 월정액·일급도 허용(tests/unit/test_obys_freelancer_pay_basis.py). 정해지지 않은 기준만 막는다.
    assert res["weekly"]["ok"] is False
    assert res["monthly"]["ok"] is True, res["monthly"]


def test_autofill_describes_hourly_fee(res):
    assert "시급 12,500원" in res["autoText"]


def test_screen_rule_matches_server_rule():
    assert 'const FREELANCER_WAGE_TYPES = ["hourly", "daily", "monthly", "case_fee"];' in CORE
    assert 'draft.wageType !== "case_fee"' not in CORE
    assert "C.FREELANCER_WAGE_TYPES.includes(v.wageType)" in EDITOR
    assert 'v.wageType !== "case_fee"' not in EDITOR
    server = (REPO / "app" / "services" / "yeoljeong_finance_service.py").read_text(encoding="utf-8")
    assert "if wage_type not in FREELANCER_WAGE_TYPES:" in server
    assert 'FREELANCER_WAGE_TYPES = ("hourly", "daily", "monthly", "case_fee")' in server


def test_switch_to_freelancer_keeps_hourly():
    assert 'if (newType === "freelancer" && E.values.wageType === "hourly") cls.wageType = "hourly";' in EDITOR

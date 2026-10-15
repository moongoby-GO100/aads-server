"""근로계약서 월급제 임금 자동 분배 — 확정 임금 → 과세 기본급·비과세 식대·기타 과세수당.

규칙 원본은 modules/contract-core.js 의 splitMonthlyWage (index.html applyRegularSalaryPreset 와 같은 규칙).
편집기(contract-editor-v41.js)는 wageSplitPatch 로 이를 입력값에 반영한다. 둘 다 node 로 실행해 검증한다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULES = REPO / "app" / "static" / "apps" / "obys" / "modules"
EDITOR = (MODULES / "contract-editor-v41.js").read_text(encoding="utf-8")

NODE_SCRIPT = r"""
globalThis.window = globalThis;
globalThis.location = { href: "https://fb.example/x", origin: "https://fb.example", search: "" };
const [coreArg, editorArg] = process.argv.slice(-2);
require(coreArg);
require(editorArg);
const C = window.ObysContractCore;
const T = window.obysContractV41._test;
const base = { wageType: "monthly", contractType: "regular", employmentTaxType: "four_insurance", wageComposition: "" };
const run = (extra, manual = []) => {
  const values = { ...base, ...extra };
  const { split, patch } = T.wageSplitPatch(values, new Set(manual), "");
  return { split, patch, merged: { ...values, ...patch } };
};
const out = {
  cash: run({ wage: "3200000", mealProvision: "cash_no_meal", taxableAllowance: "", nonTaxMealAllowance: "" }),
  meal: run({ wage: "3200000", mealProvision: "employer_meal", taxableAllowance: "", nonTaxMealAllowance: "200000" }),
  allowance: run({ wage: "3200000", mealProvision: "cash_no_meal", taxableAllowance: "100000", nonTaxMealAllowance: "" }),
  manualMeal: run({ wage: "3200000", mealProvision: "cash_no_meal", taxableAllowance: "", nonTaxMealAllowance: "150000" }, ["nonTaxMealAllowance"]),
  overCap: run({ wage: "3200000", mealProvision: "cash_no_meal", taxableAllowance: "", nonTaxMealAllowance: "300000" }, ["nonTaxMealAllowance"]),
  smallWage: run({ wage: "100000", mealProvision: "cash_no_meal", taxableAllowance: "", nonTaxMealAllowance: "" }),
  negative: run({ wage: "3200000", mealProvision: "cash_no_meal", taxableAllowance: "3100000", nonTaxMealAllowance: "" }),
  blankWage: run({ wage: "", mealProvision: "cash_no_meal", taxableAllowance: "", nonTaxMealAllowance: "" }),
  hourly: run({ wageType: "hourly", wage: "10320", mealProvision: "cash_no_meal", taxableAllowance: "", nonTaxMealAllowance: "" }),
  caseFee: run({ wageType: "case_fee", wage: "500000", mealProvision: "employer_meal" }),
  customComposition: run({ wage: "3200000", mealProvision: "cash_no_meal", wageComposition: "직접 쓴 구성 문구" }),
  stdComposition: run({ wage: "3200000", mealProvision: "cash_no_meal", wageComposition: C.defaultsFor("regular", "four_insurance").wageComposition }),
  savedCustomMeal: [...T.wageManualFor({ ...base, wage: "3200000", mealProvision: "cash_no_meal", nonTaxMealAllowance: "150000" })],
  savedAutoMeal: [...T.wageManualFor({ ...base, wage: "3200000", mealProvision: "cash_no_meal", nonTaxMealAllowance: "200000" })],
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def res() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경")
    out = subprocess.run(
        [node, "-e", NODE_SCRIPT, "--", str(MODULES / "contract-core.js"), str(MODULES / "contract-editor-v41.js")],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def _triple(case: dict) -> tuple:
    m = case["merged"]
    return (int(m["baseSalary"]), int(m["nonTaxMealAllowance"]), int(m["taxableAllowance"]))


def test_cash_meal_3_2m_splits_3_0m_200k_0(res):
    assert _triple(res["cash"]) == (3_000_000, 200_000, 0)


def test_employer_meal_has_no_non_tax_meal(res):
    assert _triple(res["meal"]) == (3_200_000, 0, 0)
    assert res["meal"]["split"]["mealLocked"] is True


def test_taxable_allowance_is_kept_and_reduces_base(res):
    assert _triple(res["allowance"]) == (2_900_000, 200_000, 100_000)


def test_manual_meal_at_or_below_cap_is_kept_but_over_cap_is_clamped(res):
    assert _triple(res["manualMeal"]) == (3_050_000, 150_000, 0)
    assert res["overCap"]["split"]["meal"] == 200_000
    assert res["overCap"]["split"]["clamped"] is True


def test_meal_never_exceeds_wage(res):
    assert _triple(res["smallWage"]) == (0, 100_000, 0)


def test_negative_base_is_error_and_leaves_base_blank(res):
    assert res["negative"]["split"]["error"]
    assert res["negative"]["patch"]["baseSalary"] == ""


def test_blank_wage_fills_nothing(res):
    assert res["blankWage"]["patch"] == {"baseSalary": ""}


def test_hourly_and_case_fee_are_not_applied(res):
    for key in ("hourly", "caseFee"):
        assert res[key]["split"] == {"applies": False}
        assert res[key]["patch"] == {}


def test_composition_text_only_replaces_blank_or_auto_text(res):
    assert res["cash"]["patch"]["wageComposition"].startswith("월 총액 3,200,000원 = 과세 기본급 3,000,000원 + 비과세 식대 200,000원")
    assert "wageComposition" not in res["customComposition"]["patch"]
    assert "wageComposition" in res["stdComposition"]["patch"]


def test_saved_draft_custom_meal_is_treated_as_manual(res):
    assert res["savedCustomMeal"] == ["nonTaxMealAllowance"]
    assert res["savedAutoMeal"] == []


def test_editor_wiring_static():
    for snippet in (
        "WAGE_TRIGGERS", "syncWageAuto", "data-cv41-wage-fit", "자동 맞춤",
        "회사가 식사를 제공하면 현금 식대는 비과세가 안 됩니다", 'id="cv41WageNote"',
    ):
        assert snippet in EDITOR, snippet
    # 월급제가 아니면 세 칸을 숨긴다.
    assert "monthlyWage || !WAGE_SPLIT_FIELDS.includes(field.name)" in EDITOR

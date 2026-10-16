"""오비서 근로계약서 자동 계산 — 기간·근로시간·주휴·월 임금·법정 문구.

계산은 modules/contract-autofill.js(순수 함수)에 있다. node 가 있으면 그 코드를 직접 실행해 검산하고,
node 가 없는 실행 환경(운영 이미지)에서는 같은 검산 수치를 파이썬 기준 계산으로 확인하고 소스에
필수 규칙이 들어 있는지 정적으로 확인한다. node 가 없다고 실패하지는 않는다.
"""
from __future__ import annotations

import calendar
import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OBYS = REPO / "app" / "static" / "apps" / "obys"
MODULES = OBYS / "modules"
AUTOFILL = (MODULES / "contract-autofill.js").read_text(encoding="utf-8")
EDITOR = (MODULES / "contract-editor-v41.js").read_text(encoding="utf-8")
ADMIN = (OBYS / "admin.html").read_text(encoding="utf-8")

MIN_WAGE = 10320


# ---------- 파이썬 기준 계산(검산표와 같은 규칙, JS 와 독립) ----------
def monthly_paid_hours(weekly_min: float) -> float:
    holiday = min(480.0, weekly_min / 5) if weekly_min >= 900 else 0.0
    exact = (weekly_min + holiday) / 60 * 365 / 7 / 12
    return float(round(exact)) if weekly_min >= 2400 else exact


def period_end(start: str, months: int) -> str:
    y, m, d = (int(x) for x in start.split("-"))
    total = m - 1 + months
    ty, tm = y + total // 12, total % 12 + 1
    dim = calendar.monthrange(ty, tm)[1]
    if d > dim:
        return date(ty, tm, dim).isoformat()
    return (date(ty, tm, d) - timedelta(days=1)).isoformat()


def test_reference_case1_weekdays_nine_to_six():
    weekly = 5 * (9 * 60 - 60)
    assert weekly == 2400
    assert min(480, weekly / 5) == 480
    hours = monthly_paid_hours(weekly)
    assert hours == 209
    assert int(MIN_WAGE * hours) == 2_156_880


def test_reference_case2_short_hours_no_holiday_pay():
    weekly = 3 * (4 * 60 + 30 - 30)
    assert weekly == 720
    hours = monthly_paid_hours(weekly)
    assert round(hours, 2) == 52.14
    assert int(MIN_WAGE * weekly / 60 * 365 / 7 / 12) == 538_114


def test_reference_case3_night_shift_split():
    net = 12 * 60 - 90
    night = 8 * 60  # 22:00~06:00 전부 근무, 휴게 시각 미입력
    assert (net, night, net - 480) == (630, 480, 150)


def test_reference_case4_monthly_split_and_hourly_equivalent():
    base = 2_500_000 - 200_000
    assert base == 2_300_000
    assert round(2_500_000 / 209, 2) == 11961.72


def test_reference_case5_period_end():
    assert period_end("2026-11-01", 3) == "2027-01-31"
    assert period_end("2026-01-31", 1) == "2026-02-28"


# ---------- 소스 정적 확인(node 없이도 실행) ----------
def test_module_is_pure_and_exports_core_api():
    for name in ("compute", "patchFor", "periodEnd", "termFromDates", "parseProbation", "parsePayDate"):
        assert re.search(rf"\b{name}\b", AUTOFILL), name
    assert "ObysContractAutofill" in AUTOFILL
    for forbidden in ("document.", "fetch(", "localStorage", "querySelector"):
        assert forbidden not in AUTOFILL, forbidden


def test_probation_text_never_contains_percent_sign():
    """서버 _probation_wage_rate 가 수습기간 칸의 'N%' 를 감액률로 읽으므로 생성 문구에는 % 가 없어야 한다."""
    match = re.search(r"function probationText\(.*?\n  \}", AUTOFILL, re.S)
    assert match and "%" not in match.group(0)


def test_admin_loads_autofill_before_editor_with_version_stamp():
    tags = re.findall(r'<script src="(modules/[^"]+)"></script>', ADMIN)
    names = [re.sub(r"\?.*", "", t) for t in tags]
    assert "modules/contract-autofill.js" in names
    assert names.index("modules/contract-schedule.js") < names.index("modules/contract-autofill.js") < names.index("modules/contract-editor-v41.js")
    assert re.search(r'modules/contract-autofill\.js\?v=[\w-]+', ADMIN)


def test_editor_uses_autofill_module_not_own_formulas():
    assert "ObysContractAutofill" in EDITOR
    assert "autofill" in EDITOR.lower()


# ---------- node 로 실제 코드 실행 ----------
NODE_SCRIPT = r"""
globalThis.window = globalThis;
const [coreArg, schedArg, autoArg] = process.argv.slice(-3);
require(coreArg); require(schedArg);
const A = require(autoArg);
const S = window.ObysContractSchedule, C = window.ObysContractCore;
const mk = (extra) => ({ ...S.emptySchedule(), ...extra });
const base = { contractType: "part_time", wageType: "hourly", wage: 10320, workplaceSizeCategory: "under_5",
  startDate: "2026-11-01", term: "none", probation: "none", payPeriod: "prev", payDay: "10", payMethod: "bank",
  splitMonthlyWage: C.splitMonthlyWage };
const wk = mk({ days: [0, 1, 2, 3, 4], start: "09:00", end: "18:00", rest: 60 });
const c1 = A.compute({ ...base, sched: wk });
const c2 = A.compute({ ...base, sched: mk({ days: [0, 2, 4], start: "18:00", end: "22:30", rest: 30 }) });
const c3 = A.compute({ ...base, sched: mk({ days: [0, 1, 2], start: "21:00", end: "09:00", rest: 90 }) });
const c4 = A.compute({ ...base, wageType: "monthly", wage: 2500000, mealProvision: "cash_no_meal", taxableAllowance: 0, sched: wk });
const c4b = A.compute({ ...base, wageType: "monthly", wage: 2500000, mealProvision: "employer_meal", taxableAllowance: 0, sched: wk });
const low = A.compute({ ...base, wage: 9000, sched: wk });
const minor = A.compute({ ...base, birthDate: "2009-05-01", sched: mk({ days: [0, 1, 2, 3, 4], start: "21:00", end: "06:00", rest: 60 }) });
const five = A.compute({ ...base, workplaceSizeCategory: "five_plus", sched: wk });
const free = A.compute({ ...base, contractType: "freelancer", sched: wk });
const patch = A.patchFor(c1, new Set(["payDate"]), "none");
const ten = mk({ days: [0, 1, 2, 3, 4], start: "09:00", end: "20:00", rest: 60 });
const ot5 = A.compute({ ...base, workplaceSizeCategory: "five_plus", sched: ten });
const ot4 = A.compute({ ...base, sched: ten });
const nightFive = A.compute({ ...base, workplaceSizeCategory: "five_plus", sched: mk({ days: [0, 1, 2], start: "21:00", end: "09:00", rest: 90 }) });
const daily = A.compute({ ...base, wageType: "daily", wage: 100000, sched: wk });
const manualSaved = A.manualFieldsOf(c1, { payDate: "매월 12일 지급", probationPeriod: "3개월 수습", payMethod: "현금", overtimeTerms: c1.texts.overtimeTerms, leaveTerms: "" });
const manualAuto = A.manualFieldsOf(c1, { payDate: c1.texts.payDate, probationPeriod: c1.texts.probationPeriod, payMethod: c1.texts.payMethod });
console.log(JSON.stringify({
  ot5: { week: ot5.time.weeklyOvertime, base: ot5.wage.base, premium: ot5.wage.premium, monthly: ot5.wage.monthly },
  ot4: { premium: ot4.wage.premium, monthly: ot4.wage.monthly },
  nightFive: { over: nightFive.time.weeklyOvertime, night: nightFive.time.weeklyNight, premium: nightFive.wage.premium },
  daily: { monthly: daily.wage.monthly, premium: daily.wage.premium, holidayPay: daily.wage.holidayPay, noted: daily.notes.some(n => n.includes("주휴수당이 들어 있지 않습니다")) },
  manual: { saved: manualSaved, auto: manualAuto },
  c1: { day: c1.time.first.net, week: c1.time.weeklyNet, holiday: c1.holiday.minutes, paid: c1.hours.paid, monthly: c1.wage.monthly, min: c1.wage.min.status },
  c2: { day: c2.time.first.net, week: c2.time.weeklyNet, holiday: c2.holiday.minutes, ultra: c2.holiday.ultraShort, paid: c2.hours.paid,
        monthly: c2.wage.monthly, notice: c2.notes.some(n => n.includes("초단시간")) },
  c3: { net: c3.time.first.net, night: c3.time.first.night, over: c3.time.first.overtime },
  c4: { split: c4.wage.split, hourly: c4.wage.baseHourly, ok: c4.wage.min.status },
  c4b: { split: c4b.wage.split },
  c5: [A.periodEnd("2026-11-01", 3), A.periodEnd("2026-01-31", 1), A.periodEnd("2026-03-15", 12)],
  low: { status: low.wage.min.status, blockers: low.blockers.length },
  minor: { minor: minor.age.minor, warnings: minor.warnings.filter(w => w.includes("18세") || w.includes("미성년")).length },
  five: { overtime: five.texts.overtimeTerms.includes("50%"), leaveHasAnnual: five.texts.leaveTerms.includes("제60조") },
  under5: { overtime: c1.texts.overtimeTerms.includes("적용되지 않는다") },
  probation: { text: A.probationText("3", "2026-11-01"), back: A.parseProbation(A.probationText("3", "2026-11-01")), none: A.parseProbation("수습기간 없음.") },
  pay: { text: A.payDateText("prev", "10"), back: A.parsePayDate(A.payDateText("prev", "last")) },
  term: [A.termFromDates("2026-11-01", "2027-01-31"), A.termFromDates("2026-11-01", ""), A.termFromDates("2026-11-01", "2027-02-10")],
  free: { applies: free.applies, hideWork: free.hideWork },
  patch: { hasEnd: "endDate" in patch, skippedManual: !("payDate" in patch), hasOvertime: "overtimeTerms" in patch }
}));
"""


@pytest.fixture(scope="module")
def node_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경 — 파이썬 기준 계산과 정적 확인으로 대체")
    out = subprocess.run(
        [node, "-e", NODE_SCRIPT, str(MODULES / "contract-core.js"), str(MODULES / "contract-schedule.js"), str(MODULES / "contract-autofill.js")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_node_case1_full_time(node_result):
    r = node_result["c1"]
    assert r == {"day": 480, "week": 2400, "holiday": 480, "paid": 209, "monthly": 2_156_880, "min": "pass"}


def test_node_case2_ultra_short(node_result):
    r = node_result["c2"]
    assert (r["day"], r["week"], r["holiday"], r["ultra"], r["notice"]) == (240, 720, 0, True, True)
    assert round(r["paid"], 2) == 52.14
    assert r["monthly"] == 538_114


def test_node_case3_night_and_overtime(node_result):
    assert node_result["c3"] == {"net": 630, "night": 480, "over": 150}


def test_node_case4_monthly_split(node_result):
    assert node_result["c4"]["split"] == {"total": 2_500_000, "base": 2_300_000, "meal": 200_000, "allowance": 0}
    assert node_result["c4"]["hourly"] == pytest.approx(2_300_000 / 209)
    assert node_result["c4b"]["split"]["base"] == 2_500_000


def test_node_case5_period_end(node_result):
    assert node_result["c5"] == ["2027-01-31", "2026-02-28", "2027-03-14"]


def test_node_min_wage_blocks_and_minor_warns(node_result):
    assert node_result["low"] == {"status": "fail", "blockers": 1}
    assert node_result["minor"]["minor"] is True and node_result["minor"]["warnings"] >= 1


def test_node_legal_texts_follow_workplace_size(node_result):
    assert node_result["five"] == {"overtime": True, "leaveHasAnnual": True}
    assert node_result["under5"]["overtime"] is True


def test_node_text_roundtrip_and_manual_protection(node_result):
    p = node_result["probation"]
    assert "%" not in p["text"] and p["back"] == "3" and p["none"] == "none"
    assert node_result["pay"]["back"] == {"period": "prev", "day": "last"}
    assert node_result["term"] == ["3m", "none", "custom"]
    assert node_result["free"] == {"applies": False, "hideWork": True}
    assert node_result["patch"] == {"hasEnd": True, "skippedManual": True, "hasOvertime": True}


# ---------- 리뷰 지적 교정: 가산수당 반영 · 일급제 주휴 표기 · 저장 문구 보호 ----------
WEEKS_PER_MONTH = 365 / 7 / 12


def test_python_reference_overtime_premium_five_plus():
    """주 5일 실근로 10시간, 시급 10,320원, 5인 이상: 월 기본 2,600,640원 + 연장 가산 224,214원."""
    weekly = 5 * 10 * 60
    assert monthly_paid_hours(weekly) == 252
    base = int(MIN_WAGE * 252)
    overtime_min = 5 * (10 * 60 - 480)
    premium = int(MIN_WAGE * 0.5 * overtime_min / 60 * WEEKS_PER_MONTH + 1e-9)
    assert (base, premium, base + premium) == (2_600_640, 224_214, 2_824_854)


def test_node_overtime_premium_is_added_only_for_five_plus(node_result):
    assert node_result["ot5"] == {"week": 600, "base": 2_600_640, "premium": 224_214, "monthly": 2_824_854}
    assert node_result["ot4"] == {"premium": 0, "monthly": 2_600_640}


def test_node_night_and_overtime_premiums_stack(node_result):
    r = node_result["nightFive"]
    assert (r["over"], r["night"]) == (450, 1440)
    assert r["premium"] == int(MIN_WAGE * 0.5 * (450 + 1440) / 60 * WEEKS_PER_MONTH + 1e-9)


def test_node_daily_wage_excludes_holiday_pay_and_reports_it_separately(node_result):
    r = node_result["daily"]
    assert r["monthly"] == 2_172_619 and r["premium"] == 0 and r["noted"] is True
    assert r["holidayPay"] == int(100_000 * WEEKS_PER_MONTH)


def test_node_saved_texts_that_cannot_be_recomputed_are_protected(node_result):
    saved = node_result["manual"]["saved"]
    assert {"payDate", "probationPeriod", "payMethod"} <= set(saved)
    assert "leaveTerms" not in saved
    assert node_result["manual"]["auto"] == []


def test_editor_summary_labels_follow_wage_type():
    assert "주휴 별도" in EDITOR and "주휴수당(별도 가산, 월)" in EDITOR
    assert "연장·야간 가산수당(월)" in EDITOR
    assert "AUTO.manualFieldsOf(" in EDITOR

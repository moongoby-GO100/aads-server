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
const five_ = { ...base, workplaceSizeCategory: "five_plus" };
const calcA = A.compute({ ...base, sched: wk });
const calcB = A.compute({ ...five_, sched: ten });
const calcC = A.compute({ ...base, wageType: "monthly", wage: 2500000, mealProvision: "cash_no_meal", taxableAllowance: 100000, sched: wk });
const calcC5 = A.compute({ ...five_, wageType: "monthly", wage: 2500000, mealProvision: "cash_no_meal", taxableAllowance: 100000, sched: ten });
const calcD = A.compute({ ...base, wageType: "daily", wage: 100000, sched: wk });
const calcShort = A.compute({ ...base, sched: mk({ days: [0, 2, 4], start: "18:00", end: "22:30", rest: 30 }) });
const rowsOf = r => Object.fromEntries(A.wageBreakdown(r).map(x => [x.key, x.amount]));
const pick = w => ({ scheduledPay: w.scheduledPay, holidayPay: w.holidayPay, overtimePremium: w.overtimePremium, nightPremium: w.nightPremium, premium: w.premium, base: w.base, monthly: w.monthly, total: w.total, weekly: w.weekly });
const rest = [239, 240, 479, 480, 509, 510, 720].map(n => S.minRest(n));
const restPlans = [S.build(mk({ days: [0], start: "09:00", end: "17:00" })), S.build(mk({ days: [0], start: "09:00", end: "21:00" })), S.build(mk({ days: [0], start: "09:00", end: "17:00", rest: 30 }))]
  .map(b => ({ rest: b.plans[0].rest, min: b.plans[0].min, net: b.plans[0].net, warned: b.warnings.some(t => t.includes("제54조")) }));
const manualSaved = A.manualFieldsOf(c1, { payDate: "매월 12일 지급", probationPeriod: "3개월 수습", payMethod: "현금", overtimeTerms: c1.texts.overtimeTerms, leaveTerms: "" });
const manualAuto = A.manualFieldsOf(c1, { payDate: c1.texts.payDate, probationPeriod: c1.texts.probationPeriod, payMethod: c1.texts.payMethod });
console.log(JSON.stringify({
  ot5: { week: ot5.time.weeklyOvertime, base: ot5.wage.base, premium: ot5.wage.premium, monthly: ot5.wage.monthly },
  ot4: { premium: ot4.wage.premium, monthly: ot4.wage.monthly },
  nightFive: { over: nightFive.time.weeklyOvertime, night: nightFive.time.weeklyNight, premium: nightFive.wage.premium },
  daily: { monthly: daily.wage.monthly, premium: daily.wage.premium, holidayPay: daily.wage.holidayPay, noted: daily.notes.some(n => n.includes("별도로 주휴수당을 더했습니다")) },
  wageA: { ...pick(calcA.wage), rows: rowsOf(calcA), text: calcA.texts.wageComposition, hours: [calcA.hours.scheduled, calcA.hours.paid, calcA.hours.holiday] },
  wageB: { ...pick(calcB.wage), rows: rowsOf(calcB), text: calcB.texts.wageComposition },
  wageC: { split: calcC.wage.split, hourly: calcC.wage.hourly, total: calcC.wage.total, premium: calcC.wage.premium, min: calcC.wage.min.status, rows: rowsOf(calcC), text: calcC.texts.wageComposition },
  wageC5: { total: calcC5.wage.total, monthly: calcC5.wage.monthly, premium: calcC5.wage.premium, rows: rowsOf(calcC5), text: calcC5.texts.wageComposition, noted: calcC5.notes.some(n => n.includes("포괄임금")) },
  wageD: { ...pick(calcD.wage), hourly: calcD.wage.hourly, avgDays: calcD.wage.avgDays, min: calcD.wage.min.status, rows: rowsOf(calcD), text: calcD.texts.wageComposition },
  wageShort: { rows: rowsOf(calcShort), text: calcShort.texts.wageComposition, total: calcShort.wage.total },
  noWage: { text: A.compute({ ...base, wage: 0, sched: wk }).texts.wageComposition, rows: A.wageBreakdown(A.compute({ ...base, wage: 0, sched: wk })).length },
  autoFields: A.AUTO_TEXT_FIELDS.includes("wageComposition"), patchHasComposition: "wageComposition" in A.patchFor(calcA, new Set(), "none"),
  patchSkipsManualComposition: !("wageComposition" in A.patchFor(calcA, new Set(["wageComposition"]), "none")),
  rest, restPlans,
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
    assert node_result["c4"]["split"] == {"total": 2_500_000, "base": 2_300_000, "meal": 200_000, "allowance": 0, "mealLocked": False}
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


def test_node_daily_wage_adds_holiday_pay_to_monthly_estimate(node_result):
    """CEO 지시(2026-10-16): 일급제 월 예상 = 근무일 × 일급 + 주휴수당. 일급에 주휴가 이미 포함된 경우는 안내 문구로만 알린다."""
    r = node_result["daily"]
    assert r["holidayPay"] == int(100_000 * WEEKS_PER_MONTH) and r["premium"] == 0 and r["noted"] is True
    assert r["monthly"] == int(100_000 * 5 * WEEKS_PER_MONTH) + r["holidayPay"] == 2_607_142


def test_node_saved_texts_that_cannot_be_recomputed_are_protected(node_result):
    saved = node_result["manual"]["saved"]
    assert {"payDate", "probationPeriod", "payMethod"} <= set(saved)
    assert "leaveTerms" not in saved
    assert node_result["manual"]["auto"] == []


def test_editor_summary_labels_follow_wage_type():
    assert "AUTO.wageBreakdown(" in EDITOR and 'class="cv41-sum-wage"' in EDITOR
    assert "AUTO.manualFieldsOf(" in EDITOR
    for label in ("기본임금", "주휴수당", "연장 가산수당", "야간 가산수당", "월 예상 총액", "1주 예상액", "환산 시급", "시간당 환산", "해당 없음(5인 미만)"):
        assert label in AUTOFILL, label


# ---------- 임금 입력란 기준 자동 계산 (CEO 지시 2026-10-16) ----------
# 월 환산 시간: 소정 = round(주 소정h × 365/7/12), 유급 = round((주 소정h + 주휴h) × 365/7/12) (주 40h 이상은 정수 반올림, 209시간 관행)
# (a) 시급 10,320 · 주 5일 09:00~18:00 휴게 60분 → 소정 174h, 주휴 35h, 유급 209h
#     기본임금 10,320×174 = 1,795,680 / 주휴수당 10,320×35 = 361,200 / 월 예상 총액 = 2,156,880 (= 10,320×209)
# (b) 5인 이상 · 09:00~20:00 휴게 60분 주 5일 → 1일 실근로 10h, 1일 연장 2h × 5일 = 주 연장 10h(600분)
#     유급 252h → 기본(주휴 포함) 10,320×252 = 2,600,640 / 연장 가산 floor(10,320×0.5×10×365/7/12) = 224,214 / 총액 2,824,854
# (c) 월급 2,500,000 · 식사 미제공 식대 200,000 · 기타 과세수당 100,000 → 과세 기본급 2,500,000−200,000−100,000 = 2,200,000
#     환산 시급 2,500,000 ÷ 209h = 11,961.72 → 11,961원
# (d) 일급 100,000 · 주 5일 1일 8h → 월 평균 근무일 5×365/7/12 = 21.7262일, 기본임금 floor(100,000×21.7262) = 2,172,619
#     시간당 12,500 · 주휴 8h → 주휴수당 floor(12,500×8×365/7/12) = 434,523 / 월 예상 총액 2,607,142
def test_python_reference_wage_cases():
    h = monthly_paid_hours(2400)
    assert (round(2400 / 60 * WEEKS_PER_MONTH), h, h - round(2400 / 60 * WEEKS_PER_MONTH)) == (174, 209, 35)
    assert (MIN_WAGE * 174, MIN_WAGE * 35, MIN_WAGE * 209) == (1_795_680, 361_200, 2_156_880)
    assert 2_500_000 - 200_000 - 100_000 == 2_200_000
    assert int(2_500_000 / 209) == 11_961
    assert int(100_000 * 5 * WEEKS_PER_MONTH) == 2_172_619
    assert int(12_500 * 8 * WEEKS_PER_MONTH) == 434_523
    assert 2_172_619 + 434_523 == 2_607_142


def test_node_hourly_separates_base_holiday_and_total(node_result):
    a = node_result["wageA"]
    assert a["hours"] == [174, 209, 35]
    assert (a["scheduledPay"], a["holidayPay"], a["total"]) == (1_795_680, 361_200, 2_156_880)
    assert a["overtimePremium"] == 0 and a["nightPremium"] == 0
    assert a["weekly"] == int(MIN_WAGE * (2400 + 480) / 60)
    assert a["rows"]["base"] == "1,795,680원" and a["rows"]["holiday"] == "361,200원" and a["rows"]["total"] == "2,156,880원"
    assert a["rows"]["overtime"] == "해당 없음(5인 미만)" and a["rows"]["night"] == "해당 없음(5인 미만)"
    assert a["rows"]["weekly"] == f"{a['weekly']:,}원"


def test_node_hourly_five_plus_overtime_is_listed_separately(node_result):
    b = node_result["wageB"]
    assert (b["scheduledPay"] + b["holidayPay"], b["overtimePremium"], b["nightPremium"], b["total"]) == (2_600_640, 224_214, 0, 2_824_854)
    assert b["rows"]["overtime"] == "224,214원" and b["rows"]["night"] == "0원" and b["rows"]["total"] == "2,824,854원"


def test_node_monthly_base_pay_is_total_minus_meal_minus_allowance(node_result):
    c = node_result["wageC"]
    assert c["split"] == {"total": 2_500_000, "base": 2_200_000, "meal": 200_000, "allowance": 100_000, "mealLocked": False}
    assert c["hourly"] == pytest.approx(2_500_000 / 209)
    assert (c["total"], c["premium"], c["min"]) == (2_500_000, 0, "pass")
    assert c["rows"]["base"] == "2,200,000원" and c["rows"]["meal"] == "200,000원" and c["rows"]["allowance"] == "100,000원"
    assert c["rows"]["hourly"] == "11,961원" and c["rows"]["overtime"] == "해당 없음(5인 미만)"


def test_node_monthly_premium_is_shown_apart_from_the_fixed_total(node_result):
    c = node_result["wageC5"]
    hourly = 2_500_000 / 252
    assert c["monthly"] == 2_500_000
    assert c["premium"] == int(hourly * 0.5 * 10 * WEEKS_PER_MONTH + 1e-9)
    assert c["total"] == 2_500_000 + c["premium"]
    assert c["rows"]["overtime"] == f"{c['premium']:,}원" and c["noted"] is True
    assert "별도" in c["text"]


def test_node_daily_includes_holiday_pay_and_hourly_conversion(node_result):
    d = node_result["wageD"]
    assert (d["scheduledPay"], d["holidayPay"], d["total"], d["monthly"]) == (2_172_619, 434_523, 2_607_142, 2_607_142)
    assert d["hourly"] == 12_500 and d["avgDays"] == pytest.approx(5 * WEEKS_PER_MONTH) and d["min"] == "pass"
    assert d["rows"]["total"] == "2,607,142원" and d["rows"]["hourly"] == "12,500원" and d["rows"]["holiday"] == "434,523원"


def test_node_wage_composition_text_uses_computed_numbers_only(node_result):
    a = node_result["wageA"]["text"]
    for part in ("시급 10,320원", "기본임금 1,795,680원", "소정근로 174시간", "주휴수당 361,200원", "주휴 35시간", "월 예상 총액 2,156,880원", "5인 미만", "급여명세서"):
        assert part in a, part
    c = node_result["wageC"]["text"]
    for part in ("월 총액 2,500,000원", "과세 기본급 2,200,000원", "비과세 식대 200,000원", "기타 과세수당 100,000원", "환산 시급 11,961원"):
        assert part in c, part
    d = node_result["wageD"]["text"]
    for part in ("일급 100,000원", "21.73일", "기본임금 2,172,619원", "주휴수당 434,523원", "주휴 8시간", "월 4.35주", "시간당 환산 12,500원", "월 예상 총액 2,607,142원"):
        assert part in d, part
    assert "연장 가산수당 224,214원" in node_result["wageB"]["text"]
    short = node_result["wageShort"]
    assert short["rows"]["holiday"] == "해당 없음" and "해당 없음(주 15시간 미만)" in short["text"]
    assert node_result["noWage"] == {"rows": 0}  # text 는 undefined 라 JSON 에서 빠진다 = 표준 문구 유지


def test_wage_composition_is_an_auto_field_with_manual_protection(node_result):
    assert node_result["autoFields"] and node_result["patchHasComposition"] and node_result["patchSkipsManualComposition"]


def test_no_hardcoded_money_in_composition_and_breakdown_code():
    for name in ("compositionText", "wageBreakdown", "premiumSentence"):
        body = re.search(rf"function {name}\(.*?\n  \}}\n", AUTOFILL, re.S).group(0)
        assert not re.search(r"\d,\d{3}|\d{4,}원", body), name


def test_wage_input_label_follows_wage_type():
    assert re.search(r'hourly: \["시급"', EDITOR) and re.search(r'daily: \["일급"', EDITOR) and re.search(r'monthly: \["월 총액"', EDITOR)
    assert "wageLabelOf(field)" in EDITOR
    assert 'label: "확정 임금"' in EDITOR  # 서버 누락 항목 이름은 그대로 유지


# ---------- 휴게 법정 최소: 근로기준법 제54조 ----------
def test_node_min_rest_follows_labor_standards_act_54(node_result):
    """근로시간 4시간이면 30분, 8시간이면 1시간 이상. 체류(span)가 휴게를 포함하므로 240분 미만 0 / 510분 미만 30 / 그 이상 60. 12시간 근무도 60분."""
    assert node_result["rest"] == [0, 30, 30, 30, 30, 60, 60]
    r8, r12, r8short = node_result["restPlans"]
    assert (r8["rest"], r8["net"], r8["warned"]) == (30, 450, False)
    assert (r12["rest"], r12["min"], r12["net"]) == (60, 60, 660)
    assert r8short["warned"] is False


# ---------- AI 리뷰 반려 교정(라운드 1): 저장 문구 보호 · 일정 없는 월급제 문구 · 계산 불가 가산 표시 ----------
# (1) 예전 버전이 만든 월급 구성 문구는 지금 입력값으로 다시 만든 문구와 한 글자도 같을 때만 자동 문구다.
#     뒤에 '성과급 …' 같은 약정을 덧붙인 문구는 직접 수정으로 보고 autoRun 이 지우지 않는다.
# (2) 일정이 없어도 월급제는 월 총액 = 기본급 + 식대 + 수당 문구를 만든다. 월급을 바꾸면 문구도 따라간다.
# (3) 5인 이상인데 시간표가 없으면 연장·야간 가산은 '계산 불가' 다(0분·0원 확정 표시 금지). 월 예상 총액에는 가산을 넣지 않는다.
EDITOR_SCRIPT = r"""
globalThis.window = globalThis;
globalThis.location = { href: "https://fb.example/x", origin: "https://fb.example", search: "" };
const [coreArg, schedArg, autoArg, editorArg] = process.argv.slice(-4);
require(coreArg); require(schedArg); require(autoArg); require(editorArg);
const C = window.ObysContractCore, S = window.ObysContractSchedule, A = window.ObysContractAutofill;
const T = window.obysContractV41._test;
const wk = { ...S.emptySchedule(), days: [0, 1, 2, 3, 4], start: "09:00", end: "18:00", rest: 60 };
const monthly = { contractType: "part_time", employmentTaxType: "four_insurance", wageType: "monthly", wage: "2500000", mealProvision: "cash_no_meal",
  taxableAllowance: "100000", nonTaxMealAllowance: "200000", startDate: "2026-11-01", workplaceSizeCategory: "under_5", weeklyHours: "" };
const legacy = C.splitMonthlyWage({ wageType: "monthly", wage: monthly.wage, mealProvision: monthly.mealProvision, taxableAllowance: monthly.taxableAllowance, nonTaxMealAllowance: null }).composition;
const addendum = " 성과급 100,000원은 별도 지급한다.";
const reopen = (wageComposition, sched) => {
  const E = T.openState({ values: { ...monthly, wageComposition }, sched: sched || S.emptySchedule() });
  T.initAuto("saved");
  const manualAfterInit = E.autoManual.has("wageComposition");
  T.autoRun();
  return { manual: manualAfterInit, value: E.values.wageComposition };
};
const noSched = A.compute({ contractType: "part_time", wageType: "monthly", wage: 2500000, mealProvision: "cash_no_meal", taxableAllowance: 100000, workplaceSizeCategory: "under_5", splitMonthlyWage: C.splitMonthlyWage });
const edit = T.openState({ values: { ...monthly, wageComposition: "" }, sched: S.emptySchedule() });
T.initAuto("new");
T.autoRun();
const first = edit.values.wageComposition;
edit.values.wage = "3000000";
T.autoRun();
const second = edit.values.wageComposition;
const withSched = T.openState({ values: { ...monthly, wageComposition: "" }, sched: wk });
T.initAuto("new");
T.autoRun();
const schedText = withSched.values.wageComposition;
withSched.sched = S.emptySchedule();
withSched.values.wage = "2600000";
T.autoRun();
const removedText = withSched.values.wageComposition;
const fb = { contractType: "part_time", wageType: "hourly", wage: 10320, workplaceSizeCategory: "five_plus", weeklyMinutesFallback: 2400, startDate: "2026-11-01", splitMonthlyWage: C.splitMonthlyWage };
const fiveFb = A.compute(fb);
const underFb = A.compute({ ...fb, workplaceSizeCategory: "under_5" });
const fiveMonthlyFb = A.compute({ ...fb, wageType: "monthly", wage: 2500000, mealProvision: "cash_no_meal", taxableAllowance: 100000 });
const fiveSched = A.compute({ ...fb, sched: { ...S.emptySchedule(), days: [0, 1, 2, 3, 4], start: "09:00", end: "20:00", rest: 60 } });
const rows = r => Object.fromEntries(A.wageBreakdown(r).map(x => [x.key, x]));
console.log(JSON.stringify({
  legacy, addendum,
  legacyOnly: reopen(legacy), legacyPlus: reopen(legacy + addendum), legacyPlusSched: reopen(legacy + addendum, wk),
  noSchedText: noSched.texts.wageComposition, first, second, schedText, removedText,
  fiveFb: { rows: rows(fiveFb), premium: fiveFb.wage.premium, known: fiveFb.wage.premiumKnown, total: fiveFb.wage.total, text: fiveFb.texts.wageComposition, noted: fiveFb.notes.some(n => n.includes("계산하지 못했습니다")) },
  underFb: { rows: rows(underFb), noted: underFb.notes.some(n => n.includes("계산하지 못했습니다")) },
  fiveMonthlyFb: { rows: rows(fiveMonthlyFb), total: fiveMonthlyFb.wage.total, noted: fiveMonthlyFb.notes.some(n => n.includes("포괄임금")) },
  fiveSched: { rows: rows(fiveSched), known: fiveSched.wage.premiumKnown, noted: fiveSched.notes.some(n => n.includes("계산하지 못했습니다")) }
}));
"""


@pytest.fixture(scope="module")
def editor_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경 — 정적 확인으로 대체")
    out = subprocess.run(
        [node, "-e", EDITOR_SCRIPT, "--", *(str(MODULES / n) for n in ("contract-core.js", "contract-schedule.js", "contract-autofill.js", "contract-editor-v41.js"))],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_node_reopened_composition_with_appended_clause_is_not_overwritten(editor_result):
    r = editor_result
    assert r["legacyOnly"]["manual"] is False  # 예전 자동 문구와 정확히 같으면 자동 문구로 취급
    assert r["legacyOnly"]["value"] != r["legacy"]
    for key in ("legacyPlus", "legacyPlusSched"):
        assert r[key]["manual"] is True, key
        assert r[key]["value"] == r["legacy"] + r["addendum"], key
    assert "성과급 100,000원은 별도 지급한다" in r["legacyPlus"]["value"]


def test_editor_has_no_prefix_regex_that_unprotects_saved_composition():
    assert "과세 기본급 /.test(" not in EDITOR


def test_node_monthly_composition_is_built_without_a_schedule(editor_result):
    r = editor_result
    assert r["noSchedText"].startswith("월 총액 2,500,000원 = 과세 기본급 2,200,000원 + 비과세 식대 200,000원")
    assert "환산 시급" not in r["noSchedText"]  # 월 환산 시간을 모르면 환산 시급은 적지 않는다
    assert r["first"].startswith("월 총액 2,500,000원 = 과세 기본급 2,200,000원")
    assert r["second"].startswith("월 총액 3,000,000원 = 과세 기본급 2,700,000원 + 비과세 식대 200,000원")
    assert "환산 시급" in r["schedText"]
    assert r["removedText"].startswith("월 총액 2,600,000원 = 과세 기본급 2,300,000원") and "환산 시급" not in r["removedText"]


def test_node_unknown_premium_is_not_shown_as_zero(editor_result):
    five = editor_result["fiveFb"]
    for key in ("overtime", "night"):
        assert five["rows"][key]["amount"] == "계산 불가", key
        assert "0분" not in five["rows"][key]["basis"] and "0원" not in five["rows"][key]["amount"], key
    assert five["known"] is False and five["premium"] == 0 and five["noted"] is True
    assert five["total"] == 2_156_880 and five["rows"]["total"]["amount"] == "2,156,880원"
    assert "가산수당 제외" in five["rows"]["total"]["basis"] and "가산수당 제외" in five["rows"]["weekly"]["basis"]
    assert "산정하지 않았다" in five["text"]
    under = editor_result["underFb"]
    assert under["rows"]["overtime"]["amount"] == "해당 없음(5인 미만)" and under["noted"] is False


def test_node_unknown_premium_monthly_and_known_schedule(editor_result):
    m = editor_result["fiveMonthlyFb"]
    assert m["rows"]["overtime"]["amount"] == "계산 불가" and m["total"] == 2_500_000 and m["noted"] is False
    assert "가산수당 제외" in m["rows"]["total"]["basis"]
    s = editor_result["fiveSched"]
    assert s["known"] is True and s["noted"] is False
    assert s["rows"]["overtime"]["amount"] == "224,214원" and "주 연장 10시간" in s["rows"]["overtime"]["basis"]

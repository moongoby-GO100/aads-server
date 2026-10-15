"""오비서 근로계약서 근무 일정 입력기 — 요일·시각 선택값 → 계약서 문구 변환과 미리 채움/덮어쓰기 규칙.

변환 규칙은 modules/contract-schedule.js, 입력기 초기화·수정 칸 보호는 contract-editor-v41.js(_test) 에 있다.
둘 다 node 로 실행해 검증한다(화면 DOM 없이 순수 함수만).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULES = REPO / "app" / "static" / "apps" / "obys" / "modules"
V41 = (REPO / "app" / "static" / "apps" / "obys" / "mockup-v4-1.html").read_text(encoding="utf-8")
EDITOR = (MODULES / "contract-editor-v41.js").read_text(encoding="utf-8")
CSS = (MODULES / "contract-editor-v41.css").read_text(encoding="utf-8")

NODE_SCRIPT = r"""
globalThis.window = globalThis;
globalThis.location = { href: "https://fb.example/x", origin: "https://fb.example", search: "" };
const [coreArg, schedArg, editorArg] = process.argv.slice(-3);
require(coreArg);
require(schedArg);
require(editorArg);
const C = window.ObysContractCore;
const S = window.ObysContractSchedule;
const T = window.obysContractV41._test;
const mk = (extra) => ({ ...S.emptySchedule(), ...extra });
const build = (extra) => S.build(mk(extra));

const weekdays = build({ days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00" });
const short = build({ days: [0, 2, 4], start: "10:00", end: "14:00" });
const night = build({ days: [0, 1, 2, 3, 4], start: "21:00", end: "09:00" });
const restTimed = build({ days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00", restStart: "12:00" });
const shortRest = build({ days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00", rest: 30 });
const longRest = build({ days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00", rest: 90 });
const sundayWork = build({ days: [1, 2, 3, 4, 5, 6], start: "10:00", end: "18:00" });
const allWeek = build({ days: [0, 1, 2, 3, 4, 5, 6], start: "10:00", end: "18:00" });
const perDay = S.build(mk({ uniform: false, days: [0, 2], rows: { 0: { start: "10:00", end: "19:00", rest: null }, 2: { start: "14:00", end: "18:00", rest: null } } }));
const pickedHoliday = build({ days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00", holiday: 5 });
const noDays = build({ start: "10:00", end: "19:00" });
const sameTime = build({ days: [0], start: "10:00", end: "10:00" });

// 문구 → 일정 되살리기
const roundTrip = S.parse(weekdays.fields, {});
const roundTripShort = S.parse(short.fields, {});
const roundTripNight = S.parse(night.fields, {});
const roundTripPerDay = S.parse(perDay.fields, {});
const roundTripRest = S.parse(restTimed.fields, {});
const unparsable = S.parse({ workDays: "협의", workTime: "점장 지시에 따름", weeklyHours: "주 40시간 기준" }, {});

// 최저임금 검사는 '주 N시간' 형식만 읽는다
const regularDraft = (weeklyHours, baseSalary) => ({
  ...C.defaultsFor("regular", "four_insurance"), contractType: "regular", employmentTaxType: "four_insurance",
  wageType: "monthly", wage: baseSalary, baseSalary, nonTaxMealAllowance: 0, taxableAllowance: 0, mealProvision: "employer_meal",
  startDate: "2026-11-01", employeeName: "홍", employeePhone: "1", employeeBirthDate: "1990-01-01", employeeAddress: "서울",
  employerName: "x", employerRegistrationNo: "1", employerRepresentative: "y", employerAddress: "z", workplaceSizeCategory: "under_5",
  weeklyHours,
});
const minWage = {
  generated: (() => { try { C.validateContractDraft(regularDraft(weekdays.fields.weeklyHours, 1000000)); return "pass"; } catch (e) { return e.message; } })(),
  generatedShort: /^주 \d+시간( \d+분)?$/.test(short.fields.weeklyHours),
};

// 편집기 초기화: 신규 → 직전 계약서 → 유형 기본값
const partTimeValues = { contractType: "part_time", employmentTaxType: "four_insurance", ...C.defaultsFor("part_time", "four_insurance") };
const regularValues = { contractType: "regular", employmentTaxType: "four_insurance", ...C.defaultsFor("regular", "four_insurance") };
const prevValues = { contractType: "part_time", employmentTaxType: "four_insurance", ...weekdays.fields };
const initFromPrev = T.schedInitState(partTimeValues, [prevValues], "new");
const initDefaultRegular = T.schedInitState(regularValues, [], "new");
const initNothingPartTime = T.schedInitState(partTimeValues, [], "new");
const initPrevUnparsable = T.schedInitState(regularValues, [{ contractType: "part_time", workDays: "협의", workTime: "협의" }], "new");

// 저장된 초안
const savedClean = T.schedInitState({ ...partTimeValues, ...weekdays.fields }, [], "saved");
const savedEdited = T.schedInitState({ ...partTimeValues, ...weekdays.fields, restTime: "근무 중 60분, 자유 이용(직접 수정)" }, [], "saved");
const savedUnparsed = T.schedInitState({ ...partTimeValues, workDays: "협의", workTime: "점장 지시에 따름", restTime: "근무 중 30분" }, [], "saved");
const savedPlaceholders = T.schedInitState(partTimeValues, [], "saved");

// 직접 수정한 칸은 덮어쓰지 않는다
const sched = mk({ days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00" });
const patchAll = T.schedPatch(partTimeValues, sched, new Set());
const patchManual = T.schedPatch(partTimeValues, sched, new Set(["restTime", "holidays"]));
const patchIncomplete = T.schedPatch({ ...partTimeValues, restTime: "x" }, mk({ days: [0] }), new Set(["workTime"]));

const out = {
  weekdays, short, night, restTimed, shortRest, longRest, sundayWork, allWeek, perDay, pickedHoliday, noDays, sameTime,
  roundTrip, roundTripShort, roundTripNight, roundTripPerDay, roundTripRest, unparsable,
  minWage,
  initFromPrev: { ...initFromPrev, manual: [...initFromPrev.manual] },
  initDefaultRegular: { ...initDefaultRegular, manual: [...initDefaultRegular.manual] },
  initNothingPartTime: { ...initNothingPartTime, manual: [...initNothingPartTime.manual] },
  initPrevUnparsable: { ...initPrevUnparsable, manual: [...initPrevUnparsable.manual] },
  savedClean: { ...savedClean, manual: [...savedClean.manual] },
  savedEdited: { ...savedEdited, manual: [...savedEdited.manual] },
  savedUnparsed: { ...savedUnparsed, manual: [...savedUnparsed.manual] },
  savedPlaceholders: { ...savedPlaceholders, manual: [...savedPlaceholders.manual] },
  patchAll, patchManual, patchIncomplete,
  available: {
    partTime: T.schedAvailable({ contractType: "part_time" }),
    regular: T.schedAvailable({ contractType: "regular" }),
    freelancer: T.schedAvailable({ contractType: "freelancer" }),
  },
  freelancerInit: T.schedInitState({ contractType: "freelancer", employmentTaxType: "freelancer_33" }, [prevValues], "new"),
  defaultsFreelancer: C.defaultsFor("freelancer", "freelancer_33"),
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def res() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경")
    out = subprocess.run(
        [node, "-e", NODE_SCRIPT, "--", str(MODULES / "contract-core.js"), str(MODULES / "contract-schedule.js"), str(MODULES / "contract-editor-v41.js")],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


# ---------- 일정 → 문구 ----------

def test_weekdays_10_to_19_fills_every_field(res):
    f = res["weekdays"]["fields"]
    assert res["weekdays"]["ok"] is True
    assert f["workDays"] == "주 5일(월·화·수·목·금)"
    assert f["workTime"] == "10:00-19:00"
    assert f["restTime"] == "근무 중 60분, 근로자가 자유롭게 이용"
    assert f["weeklyHours"] == "주 40시간"
    assert f["holidays"] == "주휴일: 일요일(주 15시간 이상·소정근로일 개근 시 유급). 근로자의 날(5/1) 유급휴일"
    lines = f["dailyWorkSchedule"].split("\n")
    assert len(lines) == 5 and lines[0] == "월 10:00-19:00 (휴게 60분, 실근로 8시간)"
    assert res["weekdays"]["warnings"] == []


def test_rest_clock_is_used_when_rest_start_is_given(res):
    f = res["restTimed"]["fields"]
    assert f["restTime"] == "12:00-13:00 중 60분, 근로자가 자유롭게 이용"
    assert "휴게 12:00-13:00 60분" in f["dailyWorkSchedule"]


def test_three_days_four_hours_gets_30_min_rest_and_no_weekly_holiday(res):
    f = res["short"]["fields"]
    assert f["restTime"] == "근무 중 30분, 근로자가 자유롭게 이용"
    assert f["weeklyHours"] == "주 10시간 30분"
    assert f["workDays"] == "주 3일(월·수·금)"
    assert "주 소정근로 15시간 미만으로 주휴일 미적용" in f["holidays"]
    assert "주휴일:" not in f["holidays"]
    assert res["minWage"]["generatedShort"] is True


def test_night_shift_ends_next_day(res):
    f = res["night"]["fields"]
    assert f["workTime"] == "21:00-09:00 (익일 종료)"
    assert f["restTime"].startswith("근무 중 60분")
    assert res["night"]["plans"][0]["span"] == 720 and res["night"]["plans"][0]["net"] == 660
    assert f["weeklyHours"] == "주 55시간"


def test_rest_below_legal_minimum_warns_and_longer_rest_does_not(res):
    assert any("제54조 최소 60분" in w for w in res["shortRest"]["warnings"])
    assert res["shortRest"]["fields"]["restTime"] == "근무 중 30분, 근로자가 자유롭게 이용"
    assert not any("제54조" in w for w in res["longRest"]["warnings"])
    assert res["longRest"]["fields"]["weeklyHours"] == "주 37시간 30분"


def test_weekly_holiday_defaults_to_sunday_else_first_off_day_and_can_be_picked(res):
    assert res["weekdays"]["holiday"] == 6
    assert res["sundayWork"]["holiday"] == 0
    assert "주휴일: 월요일" in res["sundayWork"]["fields"]["holidays"]
    assert res["pickedHoliday"]["holiday"] == 5
    assert "주휴일: 토요일" in res["pickedHoliday"]["fields"]["holidays"]
    assert res["allWeek"]["holiday"] is None
    assert any("주휴일이 없습니다" in w for w in res["allWeek"]["warnings"])


def test_per_day_times_are_grouped_and_summed(res):
    f = res["perDay"]["fields"]
    assert f["workTime"] == "월 10:00-19:00, 수 14:00-18:00"
    assert f["restTime"] == "월 60분, 수 30분 (근무 중, 근로자가 자유롭게 이용)"
    assert f["weeklyHours"] == "주 11시간 30분"
    assert f["dailyWorkSchedule"].split("\n") == ["월 10:00-19:00 (휴게 60분, 실근로 8시간)", "수 14:00-18:00 (휴게 30분, 실근로 3시간 30분)"]


def test_incomplete_schedule_is_not_ok(res):
    assert res["noDays"]["ok"] is False and res["noDays"]["fields"] == {}
    assert res["sameTime"]["ok"] is False


def test_weekly_hours_text_is_read_by_the_minimum_wage_check(res):
    assert "최저임금" in res["minWage"]["generated"]


# ---------- 문구 → 일정 ----------

def test_generated_text_round_trips(res):
    for key, days, start, end in (("roundTrip", [0, 1, 2, 3, 4], "10:00", "19:00"), ("roundTripShort", [0, 2, 4], "10:00", "14:00"), ("roundTripNight", [0, 1, 2, 3, 4], "21:00", "09:00")):
        sched = res[key]["sched"]
        assert res[key]["ok"] is True, key
        assert sched["days"] == days and sched["start"] == start and sched["end"] == end and sched["uniform"] is True
    assert res["roundTripRest"]["sched"]["restStart"] == "12:00"
    per = res["roundTripPerDay"]["sched"]
    assert per["uniform"] is False and per["rows"]["2"]["start"] == "14:00"


def test_unreadable_text_is_not_parsed(res):
    assert res["unparsable"] == {"ok": False, "empty": False}


# ---------- 편집기: 미리 채움 ----------

def test_new_contract_prefers_previous_contract_schedule(res):
    state = res["initFromPrev"]
    assert state["source"] == "직전 계약서에서 가져옴"
    assert state["sched"]["days"] == [0, 1, 2, 3, 4]
    assert state["patch"]["weeklyHours"] == "주 40시간"


def test_new_regular_contract_without_previous_uses_type_default(res):
    state = res["initDefaultRegular"]
    assert state["source"].startswith("기본값")
    assert state["sched"]["days"] == [0, 1, 2, 3, 4] and state["sched"]["start"] == "10:00" and state["sched"]["end"] == "19:00"
    assert state["patch"]["workTime"] == "10:00-19:00"


def test_new_part_time_without_previous_is_left_blank(res):
    state = res["initNothingPartTime"]
    assert state["source"] == "" and state["patch"] == {} and state["sched"]["days"] == []


def test_unreadable_previous_contract_falls_back_to_type_default(res):
    assert res["initPrevUnparsable"]["source"].startswith("기본값")


# ---------- 편집기: 저장된 초안 ----------

def test_saved_draft_restores_picker_from_text(res):
    state = res["savedClean"]
    assert state["sched"]["days"] == [0, 1, 2, 3, 4] and state["manual"] == [] and state["patch"] == {}
    assert state["source"] == "저장된 계약서 문구에서 불러옴"


def test_saved_draft_keeps_hand_edited_field_as_manual(res):
    state = res["savedEdited"]
    assert state["sched"]["days"] == [0, 1, 2, 3, 4]
    assert state["manual"] == ["restTime"]


def test_saved_draft_with_unreadable_text_keeps_text_and_locks_fields(res):
    state = res["savedUnparsed"]
    assert state["unparsed"] is True and state["sched"]["days"] == []
    assert sorted(state["manual"]) == ["restTime", "workDays", "workTime"]
    assert state["patch"] == {}


def test_saved_draft_with_only_standard_text_is_blank_not_unparsed(res):
    state = res["savedPlaceholders"]
    assert state["unparsed"] is False and state["manual"] == []


# ---------- 직접 수정 칸 보호 ----------

def test_manual_fields_are_not_overwritten(res):
    assert set(res["patchAll"]) == {"workDays", "workTime", "restTime", "weeklyHours", "dailyWorkSchedule", "holidays"}
    assert set(res["patchManual"]) == {"workDays", "workTime", "weeklyHours", "dailyWorkSchedule"}
    assert "restTime" not in res["patchManual"] and "holidays" not in res["patchManual"]


def test_incomplete_schedule_restores_standard_text_but_not_manual_fields(res):
    patch = res["patchIncomplete"]
    assert "workTime" not in patch
    assert "근로계약서 저장 전" in patch["restTime"] or patch["restTime"] != "x"


# ---------- 프리랜서 숨김 ----------

def test_picker_is_hidden_for_freelancer(res):
    assert res["available"] == {"partTime": True, "regular": True, "freelancer": False}
    assert res["freelancerInit"]["sched"]["days"] == [] and res["freelancerInit"]["patch"] == {}
    assert "schedVisible() && SCH.GEN_FIELDS" in EDITOR
    assert 'schedAvailable(E.values)' in EDITOR


def test_freelancer_fixed_work_rule_is_kept():
    assert "const FIXED_WORK_RE = /(출퇴근|근무표|주\\s*\\d+\\s*시간|월\\/화|화\\/수|수\\/목|목\\/금|금\\/토|토\\/일|상시|교대)/;" in EDITOR


# ---------- 배선·모바일 ----------

def test_picker_module_is_loaded_before_editor_and_wired_to_work_group():
    assert V41.index("modules/contract-schedule.js?v=") < V41.index("modules/contract-editor-v41.js?v=")
    assert V41.index("modules/contract-core.js?v=") < V41.index("modules/contract-schedule.js?v=")
    assert 'key === "work" && schedVisible() ? schedHtml()' in EDITOR
    assert "initSchedule(\"saved\")" in EDITOR and "initSchedule(\"new\")" in EDITOR
    assert "다시 자동 채우기" in EDITOR and "직접 수정됨" in EDITOR


def test_touch_targets_are_at_least_44px():
    for selector in (".cv41-chip{min-width:44px;min-height:44px", ".cv41-sched-toggle{display:flex;align-items:center;gap:8px;min-height:44px", ".cv41-sched-times .field{min-height:44px"):
        assert selector in CSS, selector
    assert ".cv41-chips{display:flex;flex-wrap:wrap" in CSS

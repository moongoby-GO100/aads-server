/* 오비서 근로계약서 자동 계산 — 날짜·시각·금액만 받아 기간·근로시간·주휴·월 임금·법정 안내를 계산한다.
 *
 * 순수 함수만 둔다(DOM·서버 접근 없음). 화면(contract-editor-v41.js)과 단위 테스트(node)가 같은 코드를 쓴다.
 * 근무 일정 → 문구 변환은 contract-schedule.js 가 맡고, 여기서는 그 위에 기간·연장/야간·주휴·임금·법정 문구를 얹는다.
 *
 * 기준
 *  - 계약기간 말일: 민법 제160조(기산일 응당일의 전날, 응당일이 없으면 그 달 말일).
 *  - 월 환산: (주 소정 + 주휴) × 365 ÷ 7 ÷ 12. 주 40시간 이상은 209시간 관행대로 정수로 반올림하고,
 *    그보다 짧으면 소수 그대로 둔다(예: 주 12시간 = 52.14시간).
 *  - 주휴: 주 15시간 이상이면 min(8시간, 주 소정 ÷ 40 × 8). 15시간 미만은 초단시간으로 주휴·연차·퇴직금 미적용.
 *  - 임금 구성 문구·요약표(wageBreakdown)의 숫자는 모두 compute 의 계산값이다. 문구에 금액을 직접 적지 않는다.
 *  - 야간: 22:00~06:00. 휴게 시각을 입력하지 않으면 야간에서 휴게를 빼지 않는다(근로자에게 불리하지 않은 쪽).
 *  - 수습기간 문구에는 '%' 를 쓰지 않는다 — 서버(_probation_wage_rate)가 이 칸의 'N%' 를 감액률로 읽는다.
 */
(function (root) {
  "use strict";
  const SCH = root.ObysContractSchedule || (typeof require === "function" ? require("./contract-schedule.js") : null);
  const MIN_WAGE_2026 = 10320;
  const NON_TAX_MEAL_LIMIT = 200000;
  const WEEKS_PER_MONTH = 365 / 7 / 12;
  const DAY_MIN = 480;
  const WEEK_MIN = 2400;
  const NIGHT_WINDOWS = [[0, 360], [1320, 1800], [2760, 3240]];
  const TERM_MONTHS = { "3m": 3, "6m": 6, "1y": 12, "2y": 24 };
  const NON_WORK_TYPES = ["freelancer", "confidentiality"];
  const SIMPLE_LABOR_RE = /(홀|서빙|주방보조|설거지|배달|포장|청소|세척|잡일|단순)/;
  const AUTO_TEXT_FIELDS = ["probationPeriod", "payDate", "payMethod", "overtimeTerms", "leaveTerms", "insuranceTerms", "wageComposition"];

  const won = n => `${Math.round(Number(n) || 0).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",")}원`;
  const hm = min => (SCH ? SCH.duration(Math.round(min)) : `${Math.round(min)}분`);
  const round2 = n => Math.round(n * 100) / 100;
const hoursLabel = v => `${Number.isInteger(v) ? v : round2(v)}시간`;

  // ---------- 날짜 ----------
  function parseDate(text) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text ?? "").trim());
    if (!m) return null;
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    return d.getUTCFullYear() === Number(m[1]) && d.getUTCMonth() === Number(m[2]) - 1 && d.getUTCDate() === Number(m[3]) ? d : null;
  }
  const isoDate = d => d.toISOString().slice(0, 10);
  /** 입사일 + N개월 계약의 마지막 날(민법 제160조). 날짜가 틀리면 "". */
  function periodEnd(start, months) {
    const d = parseDate(start);
    if (!d || !months) return "";
    const y = d.getUTCFullYear();
    const total = d.getUTCMonth() + months;
    const ty = y + Math.floor(total / 12);
    const tm = ((total % 12) + 12) % 12;
    const day = d.getUTCDate();
    const dim = new Date(Date.UTC(ty, tm + 1, 0)).getUTCDate();
    return isoDate(day > dim ? new Date(Date.UTC(ty, tm, dim)) : new Date(Date.UTC(ty, tm, day - 1)));
  }
  /** 저장된 입사일·종료일이 어느 기간 선택과 같은지. 종료일 없음 → none, 어느 것과도 다르면 custom. */
  function termFromDates(start, end) {
    if (!String(end ?? "").trim()) return "none";
    for (const [key, months] of Object.entries(TERM_MONTHS)) if (periodEnd(start, months) === end) return key;
    return "custom";
  }
  function ageOn(birth, ref) {
    const b = parseDate(birth);
    const r = parseDate(ref);
    if (!b || !r || b > r) return null;
    let age = r.getUTCFullYear() - b.getUTCFullYear();
    if (r.getUTCMonth() < b.getUTCMonth() || (r.getUTCMonth() === b.getUTCMonth() && r.getUTCDate() < b.getUTCDate())) age -= 1;
    return age;
  }
  const kstToday = () => new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);

  // ---------- 시간 ----------
  const overlap = (a0, a1, b0, b1) => Math.max(0, Math.min(a1, b1) - Math.max(a0, b0));
  /** 한 근무(시작 분, 총 길이, 휴게 구간)의 야간(22~06시) 분. 휴게 구간을 모르면 빼지 않는다. */
  function nightMinutes(startMin, span, rest) {
    let night = 0;
    NIGHT_WINDOWS.forEach(([a, b]) => { night += overlap(startMin, startMin + span, a, b); });
    if (rest && rest.len > 0) {
      const rs = rest.start < startMin ? rest.start + 1440 : rest.start;
      const from = Math.max(rs, startMin);
      const to = Math.min(rs + rest.len, startMin + span);
      NIGHT_WINDOWS.forEach(([a, b]) => { night -= overlap(from, to, a, b); });
    }
    return Math.max(0, night);
  }
  function analyzeTime(built, sched) {
    const restStart = sched && sched.uniform !== false && SCH.toMin(sched.restStart) !== null ? SCH.toMin(sched.restStart) : null;
    const days = built.plans.map(p => {
      const start = SCH.toMin(p.start);
      const rest = restStart !== null && p.rest > 0 ? { start: restStart, len: p.rest } : null;
      return { idx: p.idx, net: p.net, rest: p.rest, overtime: Math.max(0, p.net - DAY_MIN), night: nightMinutes(start, p.span, rest) };
    });
    const weeklyNet = days.reduce((s, d) => s + d.net, 0);
    const dailyOver = days.reduce((s, d) => s + d.overtime, 0);
    const weeklyOver = Math.max(0, days.reduce((s, d) => s + Math.min(d.net, DAY_MIN), 0) - WEEK_MIN);
    return {
      days,
      weeklyNet,
      weeklyOvertime: dailyOver + weeklyOver,
      weeklyNight: days.reduce((s, d) => s + d.night, 0),
      maxDailyNet: Math.max(0, ...days.map(d => d.net)),
      first: days[0] || null
    };
  }

  /** 주 소정(분) → 주휴 분. 15시간 미만은 0. */
  function holidayMinutesOf(weeklyMin) {
    return weeklyMin >= 900 ? Math.min(DAY_MIN, weeklyMin / 5) : 0;
  }
  /** 월 환산 시간. 주 40시간 이상은 정수(209시간 관행), 미만은 소수 그대로. */
  function monthlyHours(weeklyMin) {
    const full = weeklyMin >= WEEK_MIN;
    const holidayMin = holidayMinutesOf(weeklyMin);
    const scheduledExact = (weeklyMin / 60) * WEEKS_PER_MONTH;
    const paidExact = ((weeklyMin + holidayMin) / 60) * WEEKS_PER_MONTH;
    const paid = full ? Math.round(paidExact) : paidExact;
    const scheduled = full ? Math.round(scheduledExact) : scheduledExact;
    return { scheduled, paid, holiday: Math.max(0, paid - scheduled), paidMin: weeklyMin + holidayMin, full };
  }
  const floorMoney = n => Math.floor(n + 1e-9);

  function parseWeeklyText(text) {
    const m = /주\s*(\d+)시간(?:\s*(\d+)분)?/.exec(String(text ?? ""));
    return m ? Number(m[1]) * 60 + Number(m[2] || 0) : null;
  }

  // ---------- 최저임금 ----------
  function referenceYear(startDate, contractDate) {
    const d = parseDate(startDate) || parseDate(contractDate);
    return d ? d.getUTCFullYear() : Number(kstToday().slice(0, 4));
  }
  /** 시급·월급 환산이 최저임금 이상인지. status: pass | fail | unknown(고시 연도 밖) | skip(계산 불가) */
  function minimumWageCheck({ wageType, wage, weeklyMinutes, startDate, contractDate }) {
    const total = Number(wage) || 0;
    const result = { status: "skip", hourly: null, min: MIN_WAGE_2026, message: "" };
    if (!(total > 0) || !(weeklyMinutes > 0) || !["hourly", "monthly"].includes(wageType)) return result;
    const hours = monthlyHours(weeklyMinutes);
    const hourly = wageType === "hourly" ? total : total / hours.paid;
    result.hourly = hourly;
    if (referenceYear(startDate, contractDate) > 2026) {
      result.status = "unknown";
      result.message = "2027년 이후 계약은 해당 연도 고시 최저임금으로 다시 확인하십시오.";
    } else if (hourly + 1e-9 < MIN_WAGE_2026) {
      result.status = "fail";
      result.message = `환산 시급 ${won(Math.floor(hourly))}은 2026년 최저임금 ${won(MIN_WAGE_2026)}보다 낮습니다.`;
    } else {
      result.status = "pass";
    }
    return result;
  }

  // ---------- 문구 ----------
  const payDayText = day => (day === "last" ? "말일" : `${day}일`);
  function payDateText(payPeriod, payDay) {
    if (!payDay) return undefined;
    const period = payPeriod === "current" ? "당월 1일~말일" : "전월 1일~말일";
    const short = payDay !== "last" && Number(payDay) >= 29 ? " 해당 일이 없는 달은 말일에 지급한다." : "";
    return `${period} 근무분을 매월 ${payDayText(payDay)}에 지급한다.${short} 지급일이 휴일이면 직전 영업일에 지급한다.`;
  }
  function payMethodText(method) {
    if (method === "bank") return "직원 본인 명의 계좌이체";
    if (method === "cash") return "현금 직접 지급(근로자 본인에게 직접 지급하고 수령 확인 후 급여명세서를 교부)";
    return undefined;
  }
  function probationText(months, start) {
    if (!months || months === "none") return "수습기간 없음.";
    const n = Number(months);
    const end = periodEnd(start, n);
    const range = end ? `(${start}~${end})` : "";
    return `입사 후 ${n}개월${range}. 수습기간 중에도 계약 임금을 감액 없이 동일하게 지급한다.`;
  }
  function parsePayDate(text) {
    const t = String(text ?? "");
    const m = /^(전월|당월) 1일~말일 근무분을 매월 (\d{1,2}일|말일)에 지급한다/.exec(t);
    if (!m) return null;
    const day = m[2] === "말일" ? "last" : String(parseInt(m[2], 10));
    return { period: m[1] === "당월" ? "current" : "prev", day };
  }
  function parseProbation(text) {
    const t = String(text ?? "").trim();
    if (!t || /^수습기간 없음\.?$/.test(t)) return "none";
    const m = /^입사 후 ([123])개월(?:\([^)]*\))?\. 수습기간 중에도 계약 임금을 감액 없이 동일하게 지급한다\.$/.exec(t);
    return m ? m[1] : null;
  }
  function parsePayMethod(text) {
    const t = String(text ?? "").trim();
    if (t === "직원 본인 명의 계좌이체") return "bank";
    if (t === payMethodText("cash")) return "cash";
    return null;
  }

  function scheduleNote(t, five) {
    if (!t) return "";
    const parts = [];
    if (t.first && t.first.overtime > 0) parts.push(`1일 연장 ${hm(t.first.overtime)}`);
    if (t.weeklyOvertime > 0) parts.push(`1주 연장 ${hm(t.weeklyOvertime)}`);
    if (t.weeklyNight > 0) parts.push(`1주 야간(22:00~06:00) ${hm(t.weeklyNight)}`);
    if (!parts.length) return " 이 계약의 소정근로에는 연장·야간근로가 포함되지 않는다.";
    return ` 이 계약의 소정 근무표에는 ${parts.join(", ")}이(가) 포함되며, ${five ? "가산수당을 포함하여" : "통상임금 기준으로"} 급여명세서에 구분해 표시한다.`;
  }
  function overtimeText(five, t) {
    if (five) {
      return `상시 5인 이상 사업장으로 연장·야간(22:00~06:00)·휴일근로에는 통상임금의 50%를 가산하여 지급한다(근로기준법 제56조). 연장근로는 근로자와 합의하고 사전 승인을 받아 실시하며 1주 12시간을 넘지 않는다.${scheduleNote(t, true)}`;
    }
    return `상시 5인 미만 사업장으로 연장·야간·휴일근로 가산수당 규정(근로기준법 제56조)은 적용되지 않는다. 다만 약정 근로시간을 넘는 실제 근로시간은 사전 승인을 받아 실시하고 통상임금으로 지급한다. 상시 5인 이상이 되면 가산수당을 적용한다.${scheduleNote(t, false)}`;
  }
  function leaveText(five, weeklyMin) {
    const tail = " 지각·조퇴·결근·병가는 근태기록에 따라 급여명세서에 반영한다.";
    if (!five) {
      return `상시 5인 미만 사업장으로 근로기준법 제60조 연차유급휴가는 적용되지 않는다. 다만 경조·질병 등 사유의 휴가는 운영에 지장이 없는 범위에서 내부 승인으로 부여할 수 있고, 상시 5인 이상이 되면 법정 연차 기준을 즉시 적용한다.${tail}`;
    }
    if (weeklyMin < 900) {
      return `주 15시간 미만 초단시간 근로자로 연차유급휴가와 주휴일은 적용되지 않는다(근로기준법 제18조 제3항).${tail}`;
    }
    return `상시 5인 이상 사업장으로 근로기준법 제60조 연차유급휴가가 적용된다. 1년 미만은 개근한 1개월마다 1일, 1년간 80% 이상 출근하면 15일을 부여하며 이후 근속에 따라 가산한다. 소정근로시간이 통상근로자보다 짧으면 그 비율에 따라 부여한다.${tail}`;
  }
  function insuranceText(monthlyScheduled) {
    if (monthlyScheduled < 60) {
      return `월 소정근로시간 60시간 미만(약 ${round2(monthlyScheduled)}시간)으로 국민연금·건강보험·고용보험은 가입 제외 대상이 될 수 있어 요건 확인 후 신고한다. 산재보험은 적용하며 근로소득 원천징수 기준에 따라 처리하고 급여명세서를 교부한다.`;
    }
    return "국민연금·건강보험·고용보험·산재보험 취득 신고 대상으로 법정 기준에 따라 처리하고, 근로소득 원천징수 기준에 따라 급여명세서를 교부한다.";
  }

  // ---------- 임금 구성 문구·요약표 ----------
  const NO_PREMIUM = "해당 없음(5인 미만)";
  const PREMIUM_UNKNOWN = "계산 불가";
  const PREMIUM_NEEDS_SCHEDULE = "근무 일정(요일·시각)을 입력하면 계산됩니다";
  function premiumSentence(w, timeInfo) {
    if (!w.premiumApplies) return "연장·야간·휴일 가산수당은 5인 미만 사업장이라 적용하지 않는다(해당 없음)";
    if (!timeInfo) return "연장·야간·휴일근로 시 통상임금의 50%를 가산하여 별도로 지급한다(근무 일정이 정해지지 않아 금액은 산정하지 않았다)";
    const sep = w.type === "monthly" ? "월 총액과 별도로 " : "";
    return `${sep}연장 가산수당 ${won(w.overtimePremium)} + 야간 가산수당 ${won(w.nightPremium)}(통상임금의 50%)`;
  }
  /** 임금 구성/공제 문구. 숫자는 모두 compute 의 계산값이다. 계산할 수 없으면 undefined(표준 문구 유지). */
  function compositionText(w, hours, timeInfo) {
    if (!(w.input > 0)) return undefined;
    const tail = " 법정 공제항목은 급여명세서에 구분하여 표시한다.";
    if (w.type === "monthly" && w.split) {
      const meal = w.split.mealLocked ? "비과세 식대 0원(사용자 식사 제공)" : `비과세 식대 ${won(w.split.meal)}(식사를 제공하지 않는 경우에만 적용)`;
      const conv = hours ? ` 월 환산 ${hoursLabel(hours.paid)} 기준 환산 시급 ${won(Math.floor(w.hourly))}.` : "";
      return `월 총액 ${won(w.split.total)} = 과세 기본급 ${won(w.split.base)} + ${meal} + 기타 과세수당 ${won(w.split.allowance)}.${conv} ${premiumSentence(w, timeInfo)}.${tail}`;
    }
    if (!hours || w.monthly === null) return undefined;
    const holiday = hours.holiday > 0 ? `${won(w.holidayPay)}(주휴 ${hoursLabel(hours.holiday)})` : "해당 없음(주 15시간 미만)";
    if (w.type === "hourly") {
      return `시급 ${won(w.hourly)} × 월 환산 ${hoursLabel(hours.paid)} = 기본임금 ${won(w.scheduledPay)}(소정근로 ${hoursLabel(hours.scheduled)}) + 주휴수당 ${holiday}. ${premiumSentence(w, timeInfo)}. 월 예상 총액 ${won(w.total)}.${tail}`;
    }
    if (w.type === "daily") {
      const dailyHoliday = w.holidayMinutes > 0
        ? `${won(w.holidayPay)}(시간당 환산 ${won(Math.floor(w.hourly))} × 주휴 ${hoursLabel(w.holidayMinutes / 60)} × 월 ${round2(WEEKS_PER_MONTH)}주)`
        : "해당 없음(주 15시간 미만)";
      return `일급 ${won(w.input)} × 월 평균 근무일 ${round2(w.avgDays)}일 = 기본임금 ${won(w.scheduledPay)} + 주휴수당 ${dailyHoliday}. 시간당 환산 ${won(Math.floor(w.hourly))}. ${premiumSentence(w, timeInfo)}. 월 예상 총액 ${won(w.total)}.${tail}`;
    }
    return undefined;
  }
  /** 요약 카드용 임금 구분표: [{key, label, basis, amount}]. 계산할 수 없으면 []. */
  function wageBreakdown(result) {
    const w = result && result.wage;
    const hours = result && result.hours;
    if (!w || !(w.input > 0) || w.monthly === null || !hours) return [];
    const timeInfo = result.time && result.time.ok ? result.time : null;
    const rows = [];
    const add = (key, label, basis, amount) => rows.push({ key, label, basis, amount });
    const holidayRow = (label, basis) => hours.holiday > 0
      ? add("holiday", label, basis, won(w.holidayPay))
      : add("holiday", label, "주 15시간 미만 초단시간", "해당 없음");
    const premiumRows = (note) => {
      if (!w.premiumApplies) {
        add("overtime", "연장 가산수당", "근로기준법 제56조", NO_PREMIUM);
        add("night", "야간 가산수당", "근로기준법 제56조", NO_PREMIUM);
        return;
      }
      if (!timeInfo) {
        add("overtime", `연장 가산수당${note}`, PREMIUM_NEEDS_SCHEDULE, PREMIUM_UNKNOWN);
        add("night", `야간 가산수당${note}`, PREMIUM_NEEDS_SCHEDULE, PREMIUM_UNKNOWN);
        return;
      }
      const weekOt = hm(timeInfo.weeklyOvertime);
      const weekNight = hm(timeInfo.weeklyNight);
      add("overtime", `연장 가산수당${note}`, `통상시급 × 50% × 주 연장 ${weekOt}`, won(w.overtimePremium));
      add("night", `야간 가산수당${note}`, `통상시급 × 50% × 주 야간 ${weekNight}`, won(w.nightPremium));
    };
    if (w.type === "hourly") {
      add("input", "시급", "입력값", won(w.input));
      add("base", "기본임금(소정근로분)", `시급 × 월 소정 ${hoursLabel(hours.scheduled)}`, won(w.scheduledPay));
      holidayRow("주휴수당", `시급 × 월 주휴 ${hoursLabel(hours.holiday)}`);
      premiumRows("");
      add("weekly", "1주 예상액", w.premiumKnown ? "주 소정 + 주휴 + 가산" : "주 소정 + 주휴(가산수당 제외)", won(w.weekly));
      add("total", "월 예상 총액", w.premiumKnown ? "기본임금 + 주휴수당 + 가산수당" : "기본임금 + 주휴수당(가산수당 제외)", won(w.total));
    } else if (w.type === "daily") {
      add("input", "일급", "입력값", won(w.input));
      add("base", "기본임금(소정근로분)", `일급 × 월 평균 근무일 ${round2(w.avgDays)}일`, won(w.scheduledPay));
      holidayRow("주휴수당", "시간당 환산 × 주휴 시간 × 월 주 수");
      premiumRows("");
      add("hourly", "시간당 환산", "일급 ÷ 1일 실근로", won(Math.floor(w.hourly)));
      add("total", "월 예상 총액", "기본임금 + 주휴수당 + 가산수당", won(w.total));
    } else {
      add("input", "월 총액(약정)", "입력값", won(w.input));
      if (w.split) {
        add("base", "기본급(과세 기본급)", "월 총액 − 비과세 식대 − 기타 과세수당", won(w.split.base));
        add("meal", "비과세 식대", w.split.mealLocked ? "사용자 식사 제공" : `식사 미제공, 상한 ${won(NON_TAX_MEAL_LIMIT)}`, won(w.split.meal));
        add("allowance", "기타 과세수당", "입력값", won(w.split.allowance));
      }
      add("hourly", "환산 시급", `월 총액 ÷ 월 환산 ${hoursLabel(hours.paid)}`, won(Math.floor(w.hourly)));
      holidayRow("주휴수당(월 총액에 포함)", "환산 시급 × 월 주휴 시간");
      premiumRows("(별도)");
      add("total", w.premiumApplies ? "월 예상 지급 총액(가산 별도)" : "월 예상 총액", !w.premiumApplies ? "월 총액" : w.premiumKnown ? "월 총액 + 가산수당" : "월 총액(가산수당 제외)", won(w.total));
    }
    return rows;
  }

  // ---------- 종합 ----------
  /**
   * 입력 → 자동 계산 결과.
   * input: { contractType, wageType, wage, mealProvision, taxableAllowance, nonTaxMealAllowance|null, workplaceSizeCategory,
   *          startDate, contractDate, endDate, term, probation, payPeriod, payDay, payMethod, sched, weeklyMinutesFallback,
   *          jobDescription, birthDate, healthCertificateValidUntil, splitMonthlyWage(fn, 선택) }
   * 반환: { applies, period, time, holiday, hours, wage, texts, age, warnings, notes, blockers }
   */
  function compute(input = {}) {
    const type = input.contractType || "part_time";
    if (NON_WORK_TYPES.includes(type)) return { applies: false, hideWork: true, texts: {}, warnings: [], notes: [], blockers: [] };
    const warnings = [];
    const notes = [];
    const blockers = [];
    const five = ["five_plus", "over_5"].includes(input.workplaceSizeCategory);
    const out = { applies: true, hideWork: false, warnings, notes, blockers, texts: {} };

    // A. 기간
    const termKey = input.term || "none";
    let endDate = "";
    if (termKey === "custom") endDate = String(input.endDate || "");
    else if (TERM_MONTHS[termKey]) endDate = periodEnd(input.startDate, TERM_MONTHS[termKey]);
    const startD = parseDate(input.startDate);
    const endD = parseDate(endDate);
    out.period = { termKey, endDate, overTwoYears: false, months: null };
    if (startD && endD) {
      if (endD < startD) warnings.push("계약 종료일이 입사일보다 빠릅니다.");
      else if (endDate > periodEnd(input.startDate, 24)) {
        out.period.overTwoYears = true;
        warnings.push("계약기간이 2년을 넘습니다. 기간제법 제4조에 따라 기간의 정함이 없는 근로계약으로 보게 될 수 있습니다.");
      }
    }
    if (type === "regular" && endDate) warnings.push("정규직 표준 특약은 '기간의 정함이 없는 근로계약'입니다. 계약기간을 정했다면 추가 특약 문구를 확인하십시오.");
    const termMonths = termKey === "custom" && startD && endD ? Math.round((endD - startD) / 86400000 / 30.4375) : TERM_MONTHS[termKey] || null;
    out.period.months = termMonths;
    const oneYearOrMore = !endDate || (termMonths !== null && termMonths >= 12);

    // B/C. 시간
    let timeInfo = null;
    let weeklyMin = 0;
    out.time = { ok: false, errors: [], plans: [] };
    if (SCH && input.sched) {
      const built = SCH.build(input.sched);
      out.time = { ok: built.ok, errors: built.errors, plans: built.plans, fields: built.fields, holidayIdx: built.holiday };
      if (built.ok) {
        timeInfo = analyzeTime(built, input.sched);
        weeklyMin = built.weeklyMinutes;
        warnings.push(...built.warnings);
        Object.assign(out.time, timeInfo);
        if (timeInfo.weeklyOvertime > 720) {
          warnings.push(five
            ? `주 연장근로 ${hm(timeInfo.weeklyOvertime)}가 법정 한도 12시간을 넘습니다(근로기준법 제53조).`
            : `주 연장근로 ${hm(timeInfo.weeklyOvertime)} — 5인 미만 사업장은 제53조 한도가 적용되지 않지만 12시간을 넘는 장시간 근로입니다. 사전 합의와 휴식을 확인하십시오.`);
        }
      }
    }
    if (!weeklyMin && input.weeklyMinutesFallback > 0) weeklyMin = input.weeklyMinutesFallback;
    out.time.weeklyMinutes = weeklyMin;

    // D. 주휴
    const holidayMin = holidayMinutesOf(weeklyMin);
    out.holiday = { minutes: holidayMin, applies: holidayMin > 0, ultraShort: weeklyMin > 0 && weeklyMin < 900 };
    if (out.holiday.ultraShort) notes.push("주 15시간 미만 초단시간 근로자입니다. 주휴·연차·퇴직금이 적용되지 않습니다.");

    // E. 월 환산 시간·임금
    const hours = weeklyMin > 0 ? monthlyHours(weeklyMin) : null;
    out.hours = hours;
    const wage = Number(input.wage) || 0;
    const wageType = input.wageType || "hourly";
    const w = {
      type: wageType, input: wage, monthly: null, total: null, base: null, scheduledPay: null, holidayPay: null,
      premium: 0, overtimePremium: 0, nightPremium: 0, premiumApplies: five, premiumKnown: !five || Boolean(timeInfo), holidayMinutes: holidayMin, weekly: null, avgDays: null,
      hourly: null, baseHourly: null, min: { status: "skip", hourly: null, min: MIN_WAGE_2026, message: "" }, split: null
    };
    // 5인 이상 사업장의 연장·야간 가산(통상임금의 50%). 연장과 야간이 겹치면 각각 가산하므로 분을 더한다. 합계를 한 번에 내림해 분리값과 1원 어긋나지 않게 한다.
    const premiumsOf = hourly => {
      if (!five || !timeInfo) return { overtime: 0, night: 0, total: 0 };
      const per = min => hourly * 0.5 * (min / 60) * WEEKS_PER_MONTH;
      const total = floorMoney(per(timeInfo.weeklyOvertime + timeInfo.weeklyNight));
      const night = Math.min(total, floorMoney(per(timeInfo.weeklyNight)));
      return { overtime: total - night, night, total };
    };
    const setPremium = p => { w.premium = p.total; w.overtimePremium = p.overtime; w.nightPremium = p.night; };
    if (hours && wage > 0) {
      if (wageType === "hourly") {
        w.hourly = wage;
        w.base = floorMoney(wage * (hours.full ? hours.paid : (hours.paidMin * 365) / 5040));
        w.holidayPay = floorMoney(wage * hours.holiday);
        w.scheduledPay = w.base - w.holidayPay;
        setPremium(premiumsOf(wage));
        w.monthly = w.base + w.premium;
        w.total = w.monthly;
        const weeklyPremium = five && timeInfo ? floorMoney(wage * 0.5 * ((timeInfo.weeklyOvertime + timeInfo.weeklyNight) / 60)) : 0;
        w.weekly = floorMoney(wage * (hours.paidMin / 60)) + weeklyPremium;
      } else if (wageType === "monthly") {
        w.hourly = wage / hours.paid;
        w.monthly = wage;
        w.holidayPay = floorMoney(w.hourly * hours.holiday);
        w.scheduledPay = floorMoney(wage) - w.holidayPay;
        setPremium(premiumsOf(w.hourly));
        w.total = w.monthly + w.premium;
        if (w.premium > 0) notes.push("월 총액에 연장·야간 수당이 이미 포함된 포괄임금이라면 계약서에 그 사실과 시간을 명시하십시오. 이 계산은 월 총액과 별도로 가산수당을 더한 예상액입니다.");
      } else if (wageType === "daily" && timeInfo && timeInfo.days.length) {
        w.avgDays = timeInfo.days.length * WEEKS_PER_MONTH;
        w.hourly = wage / (timeInfo.weeklyNet / 60 / timeInfo.days.length);
        w.scheduledPay = floorMoney(wage * w.avgDays);
        w.holidayPay = holidayMin > 0 ? floorMoney(w.hourly * (holidayMin / 60) * WEEKS_PER_MONTH) : 0;
        w.base = w.scheduledPay + w.holidayPay;
        setPremium(premiumsOf(w.hourly));
        w.monthly = w.base + w.premium;
        w.total = w.monthly;
        if (holidayMin > 0) notes.push("일급제 월 예상액에는 일급과 별도로 주휴수당을 더했습니다. 일급에 주휴수당이 이미 포함돼 있다면 그 사실을 계약서에 명시하십시오.");
      }
      w.min = minimumWageCheck({ wageType, wage, weeklyMinutes: weeklyMin, startDate: input.startDate, contractDate: input.contractDate });
      if (wageType === "daily" && w.hourly !== null && referenceYear(input.startDate, input.contractDate) <= 2026) {
        w.min = { status: w.hourly + 1e-9 < MIN_WAGE_2026 ? "fail" : "pass", hourly: w.hourly, min: MIN_WAGE_2026, message: "" };
        if (w.min.status === "fail") w.min.message = `일급 ${won(wage)}의 시간당 환산 ${won(Math.floor(w.hourly))}은 2026년 최저임금 ${won(MIN_WAGE_2026)}보다 낮습니다(주휴 별도).`;
      }
      if (w.min.status === "fail") {
        warnings.push(w.min.message);
        if (wageType !== "daily") blockers.push(w.min.message);
      } else if (w.min.status === "unknown") notes.push(w.min.message);
    }
    if (hours && wage > 0 && five && !timeInfo) {
      notes.push("상시 5인 이상 사업장이지만 근무 일정(요일·시각)이 없어 연장·야간 가산수당을 계산하지 못했습니다. 월 예상 총액에는 가산수당이 들어 있지 않습니다.");
    }
    // F. 월급 분해
    if (wageType === "monthly" && wage > 0 && typeof input.splitMonthlyWage === "function") {
      const split = input.splitMonthlyWage({
        wageType, wage, mealProvision: input.mealProvision, taxableAllowance: input.taxableAllowance, nonTaxMealAllowance: input.nonTaxMealAllowance ?? null
      });
      if (split && split.applies && !split.error) {
        w.split = { total: split.total, base: split.base, meal: split.meal, allowance: split.allowance, mealLocked: Boolean(split.mealLocked) };
        if (hours) w.baseHourly = split.base / hours.paid;
        if (w.min.status === "pass" && w.baseHourly !== null && w.baseHourly + 1e-9 < MIN_WAGE_2026) {
          notes.push(`식대·수당을 뺀 과세 기본급 기준 환산 시급은 ${won(Math.floor(w.baseHourly))}입니다. 5인 미만 정규직 계약은 저장 시 기본급 기준으로 최저임금을 다시 검사합니다.`);
        }
      } else if (split && split.error) warnings.push(split.error);
    }
    out.wage = w;

    // G. 법정 안내 문구(계산할 근거가 있을 때만)
    const texts = out.texts;
    texts.probationPeriod = input.probation ? probationText(input.probation, input.startDate) : undefined;
    texts.payDate = payDateText(input.payPeriod, input.payDay);
    if (input.payPeriod === "current" && input.payDay && input.payDay !== "last") {
      warnings.push("당월 1일~말일 근무분은 말일에 지급해야 합니다. 말일 이전 지급일을 고르면 아직 일하지 않은 기간의 임금을 미리 주는 계약이 됩니다. 지급일을 말일로 하거나 전월 정산을 고르십시오.");
    }
    texts.payMethod = payMethodText(input.payMethod);
    texts.wageComposition = compositionText(w, hours, timeInfo);
    if (weeklyMin > 0) {
      texts.overtimeTerms = overtimeText(five, timeInfo);
      texts.leaveTerms = leaveText(five, weeklyMin);
      texts.insuranceTerms = insuranceText(hours.scheduled);
      if (weeklyMin >= 900) {
        notes.push(oneYearOrMore
          ? "퇴직금 대상입니다(주 15시간 이상, 계속근로 1년 이상이 되면 발생)."
          : "계약기간이 1년 미만입니다. 갱신 등으로 계속근로가 1년을 넘으면 퇴직금 대상이 됩니다.");
      } else notes.push("퇴직금은 적용되지 않습니다(주 15시간 미만).");
    }
    out.texts.endDate = endDate;

    // H. 수습
    if (input.probation && input.probation !== "none") {
      if (SIMPLE_LABOR_RE.test(String(input.jobDescription || ""))) {
        warnings.push("홀·주방보조 등 단순노무 업무는 수습기간이라도 최저임금을 감액할 수 없습니다. 임금은 수습 중에도 동일하게 지급하는 것으로 작성됩니다.");
      } else if (oneYearOrMore) {
        notes.push("1년 이상 계약이면 수습 3개월 이내에 한해 최저임금의 90%까지 감액할 수 있으나(최저임금법 제5조 제2항) 기본은 감액 없이 작성됩니다.");
      } else {
        warnings.push("계약기간이 1년 미만이면 수습기간에도 최저임금을 감액할 수 없습니다.");
      }
      if (termMonths !== null && termMonths <= Number(input.probation)) warnings.push("수습기간이 계약기간과 같거나 깁니다. 기간을 확인하십시오.");
    }

    // I. 나이
    const ref = input.startDate || input.contractDate || kstToday();
    const years = ageOn(input.birthDate, ref);
    out.age = { years, minor: years !== null && years < 18 };
    if (out.age.minor) {
      warnings.push(`만 ${years}세 미성년자입니다. 친권자(후견인) 동의서와 연락처가 필요하고 연소근로자 보호 기준이 적용됩니다.`);
      if (timeInfo) {
        if (timeInfo.maxDailyNet > 420) warnings.push("18세 미만은 1일 7시간을 넘겨 근로할 수 없습니다(근로기준법 제69조).");
        if (weeklyMin > 2100) warnings.push("18세 미만은 1주 35시간을 넘겨 근로할 수 없습니다(근로기준법 제69조).");
        if (timeInfo.weeklyNight > 0) warnings.push("18세 미만은 22시~06시 근로가 제한됩니다. 본인 동의와 고용노동부 장관 인가가 있어야 합니다(근로기준법 제70조).");
        if (out.time.holidayIdx === null) warnings.push("18세 미만은 휴일근로가 제한됩니다. 쉬는 요일을 하루 이상 두십시오(근로기준법 제70조).");
      }
    }
    // J. 보건증
    const cert = parseDate(input.healthCertificateValidUntil);
    if (cert && startD && cert < startD) warnings.push(`보건증 유효기한(${input.healthCertificateValidUntil})이 입사일(${input.startDate})보다 빨라 입사 전에 만료됩니다. 갱신본을 받으십시오.`);

    out.warnings = [...new Set(warnings)];
    out.notes = [...new Set(notes)];
    out.blockers = [...new Set(blockers)];
    return out;
  }

  /** 자동 채움 대상 칸의 새 값. 직접 수정한 칸(manual)과 계산할 수 없는 칸(undefined)은 포함하지 않는다. endDate 는 기간이 '직접 종료일'이 아닐 때만 채운다. */
  function patchFor(result, manual, termKey) {
    const patch = {};
    if (!result || !result.applies) return patch;
    if (termKey !== "custom") patch.endDate = result.texts.endDate || "";
    AUTO_TEXT_FIELDS.forEach(name => {
      if (manual && manual.has(name)) return;
      if (result.texts[name] !== undefined) patch[name] = result.texts[name];
    });
    return patch;
  }

  /** 저장된 문구 중 자동 문구와 다르거나, 선택값을 되살리지 못해 자동 문구를 만들 수 없는 칸. 이 칸들은 확인 없이 덮어쓰지 않는다. */
  function manualFieldsOf(result, values) {
    const names = [];
    if (!result || !result.applies) return names;
    AUTO_TEXT_FIELDS.forEach(name => {
      const current = String(values?.[name] ?? "").trim();
      if (current && (result.texts[name] === undefined || current !== result.texts[name])) names.push(name);
    });
    return names;
  }

  const api = {
    MIN_WAGE_2026, NON_TAX_MEAL_LIMIT, TERM_MONTHS, AUTO_TEXT_FIELDS, NON_WORK_TYPES,
    compute, patchFor, manualFieldsOf, wageBreakdown, periodEnd, termFromDates, ageOn, nightMinutes, monthlyHours, holidayMinutesOf, minimumWageCheck, parseWeeklyText,
    payDateText, payMethodText, probationText, parsePayDate, parseProbation, parsePayMethod, won
  };
  root.ObysContractAutofill = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

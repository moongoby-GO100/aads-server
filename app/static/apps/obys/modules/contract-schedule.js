/* 오비서 근로계약서 근무 일정 변환 — 요일·시각 선택값 ↔ 계약서 문구(근무일·근무시간·휴게·주 소정근로·근로일별·휴일).
 *
 * 화면(contract-editor-v41.js)과 단위 테스트(node)가 같은 코드를 쓴다. DOM·서버 접근 없음.
 * 휴게 최소값: 근로기준법 제54조 — 근무 4시간 이상 30분, 8시간 이상 60분.
 * 주 소정근로시간 문구는 반드시 '주 N시간' 또는 '주 N시간 M분'이다(contract-core.js 최저임금 검사가 이 형식만 읽는다).
 */
(function (root) {
  "use strict";
  const DAYS = ["월", "화", "수", "목", "금", "토", "일"];
  const GEN_FIELDS = ["workDays", "workTime", "restTime", "weeklyHours", "dailyWorkSchedule", "holidays"];
  const LABOR_DAY = "근로자의 날(5/1) 유급휴일";
  const NEXT_DAY = "익일 종료";

  const pad = n => String(n).padStart(2, "0");
  function toMin(text) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(text ?? "").trim());
    if (!m) return null;
    const h = Number(m[1]);
    const mi = Number(m[2]);
    return h > 23 || mi > 59 ? null : h * 60 + mi;
  }
  function clock(min) {
    const v = ((min % 1440) + 1440) % 1440;
    return `${pad(Math.floor(v / 60))}:${pad(v % 60)}`;
  }
  function duration(min) {
    const h = Math.floor(min / 60);
    const m = min % 60;
    return h && m ? `${h}시간 ${m}분` : h ? `${h}시간` : `${m}분`;
  }
  /** 시작→종료 분. 종료가 시작보다 이르면 다음날 종료(야간). 같으면 0. 시각이 틀리면 null. */
  function spanMinutes(start, end) {
    const a = toMin(start);
    const b = toMin(end);
    if (a === null || b === null) return null;
    if (a === b) return 0;
    return b > a ? b - a : b + 1440 - a;
  }
  /** 근로기준법 제54조: 근로시간(휴게 제외)이 4시간이면 30분, 8시간이면 1시간 이상을 근로시간 도중에 준다. span 은 휴게를 포함한 체류 분.
   *  휴게 r 을 주고 남은 근로시간 span-r 이 8시간 이상이면 60분, 4시간 이상이면 30분이 필요하므로 체류 240분 미만 0 / 510분 미만 30 / 그 이상 60. 12시간 근무도 법정 최소는 60분. */
  function minRest(span) { return span >= 510 ? 60 : span >= 240 ? 30 : 0; }
  function normRest(value) {
    if (value === null || value === undefined || String(value).trim() === "") return null;
    const n = Math.round(Number(value));
    return Number.isFinite(n) && n >= 0 ? n : null;
  }
  function sortedDays(days) {
    return [...new Set((days || []).map(Number))].filter(i => Number.isInteger(i) && i >= 0 && i < 7).sort((a, b) => a - b);
  }
  /** 주휴일 기본값: 일요일이 비면 일요일, 아니면 첫 비근무일. 7일 모두 근무면 null. */
  function defaultHoliday(days) {
    const worked = sortedDays(days);
    if (worked.length >= 7) return null;
    if (!worked.includes(6)) return 6;
    for (let i = 0; i < 7; i += 1) if (!worked.includes(i)) return i;
    return null;
  }
  /** 사용자가 고른 주휴일(근무하지 않는 요일일 때만 유효), 아니면 기본값. */
  function effectiveHoliday(s) {
    const days = sortedDays(s.days);
    const chosen = s.holiday === null || s.holiday === undefined || s.holiday === "" ? null : Number(s.holiday);
    return chosen !== null && Number.isInteger(chosen) && chosen >= 0 && chosen < 7 && !days.includes(chosen) ? chosen : defaultHoliday(days);
  }
  function emptySchedule() {
    return { uniform: true, days: [], start: "", end: "", rest: null, restStart: "", rows: {}, holiday: null };
  }
  function defaultScheduleFor(contractType) {
    if (contractType === "regular" || contractType === "manager") {
      return { ...emptySchedule(), days: [0, 1, 2, 3, 4], start: "10:00", end: "19:00" };
    }
    return null;
  }

  function group(items, keyOf) {
    const map = new Map();
    items.forEach(item => {
      const key = keyOf(item);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(item);
    });
    return [...map.entries()];
  }
  const names = plans => plans.map(p => DAYS[p.idx]).join("·");
  const hoursText = min => {
    const h = Math.floor(min / 60);
    const m = min % 60;
    return m ? `주 ${h}시간 ${m}분` : `주 ${h}시간`;
  };

  /** 일정 → { ok, errors, warnings, plans, weeklyMinutes, holiday, fields }. fields 는 GEN_FIELDS 문구. */
  function build(s) {
    const errors = [];
    const warnings = [];
    const uniform = s.uniform !== false;
    const days = sortedDays(s.days);
    const plans = [];
    if (!days.length) errors.push("근무 요일을 한 개 이상 선택하십시오.");
    const uniformRest = normRest(s.rest);
    let timeErrorShown = false;
    days.forEach(idx => {
      const row = uniform ? {} : ((s.rows || {})[idx] || {});
      const start = uniform ? s.start : (row.start || s.start);
      const end = uniform ? s.end : (row.end || s.end);
      const label = uniform ? "" : `${DAYS[idx]}요일 `;
      const span = spanMinutes(start, end);
      if (span === null || span === 0) {
        if (!(uniform && timeErrorShown)) errors.push(span === 0 ? `${label}시작과 종료 시각이 같습니다.` : `${label}시작·종료 시각을 입력하십시오.`);
        timeErrorShown = true;
        return;
      }
      const min = minRest(span);
      const rest = normRest(uniform ? uniformRest : row.rest) ?? min;
      if (rest >= span) {
        errors.push(`${label}휴게시간이 근무시간 이상입니다.`);
        return;
      }
      const net = span - rest;
      if (rest < min && !(uniform && plans.length)) {
        warnings.push(`${label}휴게 ${rest}분은 근로기준법 제54조 최소 ${min}분보다 짧습니다(근무 ${duration(span)}).`);
      }
      if (net > 480 && !(uniform && plans.length)) warnings.push(`${label}1일 소정근로(${duration(net)})가 8시간을 넘습니다. 연장근로 합의·가산수당을 확인하십시오.`);
      plans.push({ idx, start: clock(toMin(start)), end: clock(toMin(end)), span, min, rest, net, overnight: toMin(end) < toMin(start) });
    });
    if (errors.length) return { ok: false, errors, warnings, plans, weeklyMinutes: 0, holiday: null, fields: {} };

    const weeklyMinutes = plans.reduce((sum, p) => sum + p.net, 0);
    if (weeklyMinutes > 2400) warnings.push(`주 소정근로(${duration(weeklyMinutes)})가 법정 40시간을 넘습니다.`);
    const holiday = effectiveHoliday(s);
    if (holiday === null) warnings.push("7일 모두 근무로 선택돼 주휴일이 없습니다. 주 1일 이상 쉬는 요일이 필요합니다.");

    let restClock = "";
    if (uniform && plans[0].rest > 0 && toMin(s.restStart) !== null) {
      const first = plans[0];
      const rs = toMin(s.restStart);
      const offset = (rs - toMin(first.start) + 1440) % 1440;
      if (offset + first.rest > first.span) warnings.push("휴게 시각이 근무시간 밖에 걸쳐 있습니다.");
      restClock = `${clock(rs)}-${clock(rs + first.rest)}`;
    }

    const over = p => (p.overnight ? ` (${NEXT_DAY})` : "");
    const fields = {};
    fields.workDays = `주 ${plans.length}일(${names(plans)})`;
    fields.workTime = uniform
      ? `${plans[0].start}-${plans[0].end}${over(plans[0])}`
      : group(plans, p => `${p.start}-${p.end}`).map(([, list]) => `${names(list)} ${list[0].start}-${list[0].end}${over(list[0])}`).join(", ");
    if (uniform) {
      const rest = plans[0].rest;
      fields.restTime = rest === 0
        ? (plans[0].min === 0 ? "휴게시간 없음(1일 근로시간 4시간 미만)" : "휴게시간 없음")
        : `${restClock ? `${restClock} 중` : "근무 중"} ${rest}분, 근로자가 자유롭게 이용`;
    } else {
      const groups = group(plans, p => p.rest);
      fields.restTime = groups.length === 1
        ? (groups[0][0] === 0 ? "휴게시간 없음" : `근무 중 ${groups[0][0]}분, 근로자가 자유롭게 이용`)
        : `${groups.map(([rest, list]) => `${names(list)} ${rest}분`).join(", ")} (근무 중, 근로자가 자유롭게 이용)`;
    }
    fields.weeklyHours = hoursText(weeklyMinutes);
    fields.dailyWorkSchedule = plans.map(p => {
      const rest = p.rest === 0 ? "휴게 없음" : `휴게 ${uniform && restClock ? `${restClock} ` : ""}${p.rest}분`;
      return `${DAYS[p.idx]} ${p.start}-${p.end}${over(p)} (${rest}, 실근로 ${duration(p.net)})`;
    }).join("\n");
    if (weeklyMinutes < 900) fields.holidays = `주 소정근로 15시간 미만으로 주휴일 미적용. ${LABOR_DAY}`;
    else if (holiday === null) fields.holidays = "";
    else fields.holidays = `주휴일: ${DAYS[holiday]}요일(주 15시간 이상·소정근로일 개근 시 유급). ${LABOR_DAY}`;
    return { ok: true, errors, warnings, plans, weeklyMinutes, holiday, fields };
  }

  // ---------- 문구 → 일정 ----------
  function parseDays(text) {
    const set = new Set();
    const cleaned = String(text ?? "").replace(/요일/g, "").replace(/([월화수목금토일])\s*~\s*([월화수목금토일])/g, (all, a, b) => {
      const from = DAYS.indexOf(a);
      const to = DAYS.indexOf(b);
      for (let i = from; ; i = (i + 1) % 7) { set.add(i); if (i === to) break; }
      return " ";
    });
    for (const ch of cleaned) { const i = DAYS.indexOf(ch); if (i >= 0) set.add(i); }
    return sortedDays([...set]);
  }
  function parseWorkDays(text) {
    const t = String(text ?? "").trim();
    const m = /주\s*(\d)\s*일\s*\(([^)]*)\)/.exec(t);
    if (m) {
      const days = parseDays(m[2]);
      return days.length === Number(m[1]) ? days : [];
    }
    const plain = t.replace(/요일/g, "");
    return /^[월화수목금토일\s·,/~]+$/.test(plain) ? parseDays(plain) : [];
  }
  const DAILY_LINE = /^\s*([월화수목금토일](?:\s*[·,/]\s*[월화수목금토일])*(?:\s*~\s*[월화수목금토일])?)(?:요일)?\s+(\d{1,2}:\d{2})\s*[-~–]\s*(\d{1,2}:\d{2})(.*)$/;
  function parseDaily(text) {
    const rows = {};
    const lines = String(text ?? "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (!lines.length) return null;
    for (const line of lines) {
      const m = DAILY_LINE.exec(line);
      if (!m || toMin(m[2]) === null || toMin(m[3]) === null) return null;
      const days = parseDays(m[1]);
      if (!days.length) return null;
      const rest = /휴게\s*(?:\d{1,2}:\d{2}\s*[-~–]\s*\d{1,2}:\d{2}\s*)?(\d+)\s*분/.exec(m[4]);
      days.forEach(idx => { rows[idx] = { start: clock(toMin(m[2])), end: clock(toMin(m[3])), rest: rest ? Number(rest[1]) : null }; });
    }
    return rows;
  }
  /** 표준 안내문(placeholders)과 같은 칸은 '문구 없음'으로 본다. */
  function realValues(values, placeholders = {}) {
    const out = {};
    GEN_FIELDS.forEach(name => {
      const value = String(values?.[name] ?? "").trim();
      out[name] = value && value !== String(placeholders[name] ?? "").trim() ? value : "";
    });
    return out;
  }
  /** 저장된 문구 → 일정. 읽지 못하면 { ok:false, empty } (empty=true 면 읽을 문구 자체가 없음). */
  function parse(values, placeholders = {}) {
    const f = realValues(values, placeholders);
    if (!GEN_FIELDS.some(name => f[name])) return { ok: false, empty: true };
    const days = parseWorkDays(f.workDays);
    if (!days.length) return { ok: false, empty: false };
    const s = { ...emptySchedule(), days };

    let rows = f.dailyWorkSchedule ? parseDaily(f.dailyWorkSchedule) : null;
    if (rows && (Object.keys(rows).length !== days.length || !days.every(i => rows[i]))) rows = null;
    if (rows) {
      const list = days.map(i => rows[i]);
      const same = list.every(r => r.start === list[0].start && r.end === list[0].end);
      if (same) {
        s.start = list[0].start;
        s.end = list[0].end;
        const restSame = list.every(r => r.rest === list[0].rest);
        const min = minRest(spanMinutes(s.start, s.end));
        if (restSame && list[0].rest !== null && list[0].rest !== min) s.rest = list[0].rest;
      } else {
        s.uniform = false;
        days.forEach(i => {
          const r = rows[i];
          const min = minRest(spanMinutes(r.start, r.end));
          s.rows[i] = { start: r.start, end: r.end, rest: r.rest !== null && r.rest !== min ? r.rest : null };
        });
        s.start = rows[days[0]].start;
        s.end = rows[days[0]].end;
      }
    } else {
      const found = [...String(f.workTime).matchAll(/(\d{1,2}:\d{2})\s*[-~–]\s*(\d{1,2}:\d{2})/g)].map(m => [clock(toMin(m[1])), clock(toMin(m[2]))]);
      const distinct = new Set(found.map(pair => pair.join("-")));
      if (found.some(pair => toMin(pair[0]) === null) || distinct.size !== 1) return { ok: false, empty: false };
      [s.start, s.end] = found[0];
      const rest = /(\d+)\s*분/.exec(f.restTime);
      if (rest && Number(rest[1]) !== minRest(spanMinutes(s.start, s.end))) s.rest = Number(rest[1]);
    }
    if (s.uniform) {
      const clockRest = /(\d{1,2}:\d{2})\s*-\s*\d{1,2}:\d{2}\s*중/.exec(f.restTime);
      if (clockRest) s.restStart = clock(toMin(clockRest[1]));
    }
    const hol = /주휴일:\s*([월화수목금토일])요일/.exec(f.holidays);
    if (hol && !days.includes(DAYS.indexOf(hol[1]))) s.holiday = DAYS.indexOf(hol[1]);
    return build(s).ok ? { ok: true, sched: s } : { ok: false, empty: false };
  }
  /** 저장된 문구 중 입력기가 만든 문구와 다른 칸(= 사람이 고친 칸) 이름. */
  function reconcile(sched, values, placeholders = {}) {
    const manual = new Set();
    const built = build(sched);
    const f = realValues(values, placeholders);
    GEN_FIELDS.forEach(name => { if (f[name] && (!built.ok || f[name] !== built.fields[name])) manual.add(name); });
    return manual;
  }
  /** 자동 채움 대상 칸의 새 값. 직접 수정한 칸(manual)은 건드리지 않는다. 일정이 불완전하면 fallback(표준 안내문)으로 되돌린다. */
  function patchFor(sched, manual, fallback = {}) {
    const built = build(sched);
    const patch = {};
    GEN_FIELDS.forEach(name => {
      if (manual && manual.has(name)) return;
      patch[name] = built.ok ? built.fields[name] : String(fallback[name] ?? "");
    });
    return patch;
  }

  const api = { DAYS, GEN_FIELDS, build, parse, reconcile, realValues, patchFor, emptySchedule, defaultScheduleFor, defaultHoliday, effectiveHoliday, minRest, spanMinutes, toMin, clock, duration, normRest };
  root.ObysContractSchedule = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);

// v4 관리자 근태 관리 목업 로컬 검증(v3 6화면 + 직원 목록·직원 상세·근무 일정·출근/결근 처리). 서비스/API 에 접속하지 않는다(file:// 만 연다).
// 사용: node assets/verify.cjs  → evidence/*.png, evidence/playwright-results.json
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const SCREENS = {
 onboarding: 'obys-attendance-admin-onboarding', today: 'obys-attendance-admin-today', branches: 'obys-attendance-admin-branches',
 employees: 'obys-attendance-admin-employees', calendar: 'obys-attendance-admin-employee-calendar', stats: 'obys-attendance-admin-stats',
 roster: 'obys-attendance-admin-roster', detail: 'obys-attendance-admin-employee-detail', schedule: 'obys-attendance-admin-schedule', edit: 'obys-attendance-admin-attendance-edit'
};
const STATES = ['loading', 'empty', 'error', 'permission', 'offline'];
const TAP = 'button,a[href],select,input:not([type=checkbox]):not([type=hidden]),textarea,summary,label.check,label.chip';
(async () => {
 const browser = await chromium.launch({executablePath: process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell', headless: true, args: ['--no-sandbox']});
 const results = [];
 try {
 for (const [viewport, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
  const ctx = await browser.newContext({viewport: {width, height}, locale: 'ko-KR', timezoneId: 'Asia/Seoul'});
  const page = await ctx.newPage();
  const errors = [], requests = [], captures = [], overflow = [], small = [], checks = [], links = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('request', r => { if (/^https?:/.test(r.url())) requests.push(r.url()); });
  const open = async (key, q = '') => { await page.goto('file://' + path.join(root, SCREENS[key] + '.html') + q); };
  const text = sel => page.locator(sel).first().innerText();
  const hid = sel => page.locator(sel).first().evaluate(e => e.hidden);
  const cap = async (key, state) => {
   const over = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
   if (over > 0) overflow.push({screen: key, state, over});
   const tiny = await page.evaluate(sel => [...document.querySelectorAll(sel)].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden' && !e.closest('dialog:not([open])') && (r.height < 43.5 || r.width < 43.5); }).map(e => `${e.tagName}#${e.id || ''}:${(e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 20)}:${Math.round(e.getBoundingClientRect().width)}x${Math.round(e.getBoundingClientRect().height)}`), TAP);
   small.push(...tiny.map(t => ({screen: key, state, target: t})));
   const file = `${key}-${viewport}-${state}.png`;
   await page.screenshot({path: path.join(root, 'evidence', file), fullPage: true});
   captures.push({screen_id: SCREENS[key], state, file});
  };
  // 공통: 5상태 캡처·복구, 세션 만료 → 로그인 → 복귀, 내비 링크 실재
  const common = async (key, q = '') => {
   await open(key, q);
   for (const state of STATES) {
    await page.locator('#scenario').selectOption(state);
    assert.equal(await hid('#live'), true); assert.equal(await hid('#stateBox'), false);
    assert.equal(await page.locator('#recover').count(), 1);
    await cap(key, state);
    await page.locator('#recover').click();
    assert.equal(await hid('#live'), false); assert.equal(await hid('#stateBox'), true);
    assert.equal(await page.locator('#scenario').inputValue(), 'default');
   }
   await page.locator('#scenario').selectOption('session-expired');
   await page.waitForURL(/\/login\.html\?return=/);
   assert.match(await text('h1'), /^로그인$/);
   assert.equal(await page.locator('#stateBox').count(), 0);
   await page.locator('#login').click();
   await page.waitForURL(u => u.pathname.endsWith(SCREENS[key] + '.html'));
   if (q) assert.equal(new URL(page.url()).search, q);
   const navs = await page.locator('nav.side a').evaluateAll(es => es.map(e => e.getAttribute('href')));
   assert.equal(navs.length, 10);
   for (const h of navs) assert.ok(fs.existsSync(path.resolve(root, h.split('#')[0].split('?')[0])), h);
   checks.push(key + ':five-states+session-redirect+nav');
  };

  // ---- A 근태 시작하기
  await open('onboarding');
  assert.equal(await page.locator('.steps li').count(), 3);
  assert.match(await text('#mRec'), /0건/); assert.match(await text('#mCoord'), /0 \/ 5/); assert.match(await text('#mAddr'), /2 \/ 5/);
  assert.match(await text('#warn'), /좌표 미설정 지점 5곳/);
  assert.equal(await page.locator('#go1').getAttribute('href'), 'obys-attendance-admin-branches.html');
  await cap('onboarding', 'default');
  await page.locator('#toggleUnset').click();
  assert.equal(await page.locator('#unsetRows tr').count(), 5);
  assert.equal(await page.locator('#unsetRows .badge.err').count(), 3);
  await page.locator('#openRecs').click(); assert.match(await text('#recs'), /아직 근태 기록이 없습니다/);
  await page.locator('#copy').click(); assert.match(await text('#mShare'), /공유함/);
  await cap('onboarding', 'checklist-progress');
  await common('onboarding');
  checks.push('A:checklist-3-steps', 'A:coords-0-of-5', 'A:records-0');

  // ---- B 오늘 현황
  await open('today');
  assert.match(await text('#mIn'), /6명 \/ 8/); assert.match(await text('#mNone'), /2명/); assert.match(await text('#mWork'), /6명/);
  const inboxN = await page.locator('#inbox .item').count(); assert.ok(inboxN >= 3);
  const inboxText = await text('#inbox'); for (const r of ['위치 미확인', '퇴근 누락', '좌표 미설정 지점']) assert.match(inboxText, new RegExp(r));
  assert.match(await text('#corrN'), /대기 2건/);
  await cap('today', 'default');
  await page.locator('#inbox [data-ok]').first().click(); assert.equal(await page.locator('#inbox .item').count(), inboxN - 1);
  await page.locator('[data-rj="c2"]').click(); await page.locator('#rejOk').click(); assert.match(await text('#rejErr'), /5자 이상/);
  await page.locator('#rejReason').fill('출근 시각 근거가 없어 점장 확인 후 다시 요청해 주세요.');
  await cap('today', 'correction-reject');
  await page.locator('#rejOk').click(); assert.match(await text('[data-corr="c2"]'), /반려됨[\s\S]*점장 확인/);
  await page.locator('[data-ap="c1"]').click(); assert.match(await text('[data-corr="c1"]'), /승인됨/);
  assert.equal(await page.locator('#inbox [data-rec="A|2026-10-27"]').count(), 0);
  await page.locator('#closeBtn').click(); assert.equal(await page.locator('#closeOk').isDisabled(), true);
  assert.match(await text('#closeTxt'), /정정 대기 0건/);
  await cap('today', 'period-close');
  await page.locator('#closeCk').check(); await page.locator('#closeOk').click(); assert.match(await text('#closeState'), /마감됨/);
  await common('today');
  checks.push('B:summary-4', 'B:inbox-3-reasons', 'B:reject-reason-5', 'B:approve-removes-inbox', 'B:period-close-confirm');

  // ---- F 지점·근무지 관리 (주소 → 후보 → 좌표 자동 → 반경 → 저장, 실패 복구)
  await open('branches');
  assert.match(await text('#mCoord'), /0 \/ 5/);
  assert.ok((await page.locator('[data-b]').filter({visible: true}).count()) === 5);
  await cap('branches', 'default');
  await page.locator('[data-b="b1"]').filter({visible: true}).click();
  assert.equal(await page.locator('#addr').inputValue(), '가상시 샘플구 예시로 12');
  await page.locator('#search').click();
  assert.equal(await page.locator('#cands button').count(), 3);
  await cap('branches', 'address-candidates');
  await page.locator('#cands button').first().click();
  assert.equal(await page.locator('#lat').inputValue(), '37.500101'); assert.equal(await page.locator('#lng').inputValue(), '127.000101');
  assert.match(await text('#coordSrc'), /자동 채움/);
  await page.locator('#radius').fill('55'); await page.locator('#save').click(); assert.match(await text('#radErr'), /상한 50m/);
  await cap('branches', 'radius-over-limit');
  await page.locator('#radius').fill('40'); await page.locator('#save').click();
  assert.match(await text('#mCoord'), /1 \/ 5/); assert.match(await text('#toast'), /저장 완료/);
  await cap('branches', 'saved');
  await page.locator('[data-b="b3"]').filter({visible: true}).click();
  assert.equal(await hid('#noAddr'), false); assert.equal(await page.locator('#addr').inputValue(), '');
  await page.locator('#search').click(); assert.match(await text('#searchMsg'), /4자 이상/);
  await cap('branches', 'address-required');
  await page.locator('#addr').fill('가상시 샘플구 없는길 999');
  await page.locator('#mode').selectOption('none'); await page.locator('#search').click();
  assert.match(await text('#searchMsg'), /검색 결과가 없습니다/);
  await cap('branches', 'no-result');
  await page.locator('#mode').selectOption('fail'); await page.locator('#search').click();
  assert.match(await text('#searchMsg'), /응답하지 않습니다/);
  await page.locator('#manual2').click(); assert.equal(await page.locator('#lat').evaluate(e => e.readOnly), false);
  await page.locator('#lat').fill('99'); await page.locator('#lng').fill('127.1'); await page.locator('#save').click();
  assert.match(await text('#coordErr'), /범위/);
  await cap('branches', 'api-fail-manual');
  await page.locator('#lat').fill('37.400303'); await page.locator('#save').click();
  assert.match(await text('#mCoord'), /2 \/ 5/); assert.match(await text('#mAddr'), /3 \/ 5/);
  await page.locator('[data-b="b4"]').filter({visible: true}).click();
  await page.locator('#addr').fill('가상시 견본구 시범로 5'); await page.locator('#mode').selectOption('one'); await page.locator('#search').click();
  assert.equal(await page.locator('#cands button').count(), 1);
  await common('branches');
  checks.push('F:address-search-3-candidates', 'F:auto-fill-lat-lng', 'F:radius-cap-50', 'F:address-first', 'F:no-result-recovery', 'F:api-fail-manual-entry');

  // ---- G 직원별 현황 → H
  await open('employees');
  const rowsSel = viewport === 'desktop' ? '#rows tr' : '#cards .ecard';
  assert.equal(await page.locator(rowsSel).count(), 8);
  await cap('employees', 'default');
  await page.locator('#branch').selectOption('b1'); await page.locator('#sort').selectOption('worked');
  assert.equal(await page.locator(rowsSel).count(), 3);
  await cap('employees', 'filtered');
  await page.locator('#q').fill('없는이름'); assert.match(await text('#none'), /검색 결과 없음/);
  await page.locator('#reset2').click(); assert.equal(await page.locator(rowsSel).count(), 8);
  await page.locator('#from').fill('2026-10-29'); assert.match(await text('#none'), /기간 오류/);
  await cap('employees', 'date-range-error');
  await page.locator('#reset').click();
  await page.locator(rowsSel + '[data-emp="B"] a').click();
  await page.waitForURL(/employee-detail\.html\?emp=B/);
  assert.equal(await page.locator('#emp').inputValue(), 'B'); assert.match(await text('h1'), /샘플 직원 B/);
  links.push('G->K'); checks.push('G:8-employees', 'G:branch-filter-sort', 'G:search-empty', 'G:date-range-error', 'G:row->K');
  await common('employees');

  // ---- H 출근 달력
  await open('calendar', '?emp=A');
  assert.match(await text('#month'), /2026년 10월/);
  assert.equal(await page.locator('#cal button').count(), 31);
  assert.equal(await page.locator('#cal button.c-missing[data-date="2026-10-27"]').count(), 1);
  assert.equal(await page.locator('#cal button.c-working[data-date="2026-10-28"]').count(), 1);
  assert.equal(await page.locator('#next').isDisabled(), true);
  await cap('calendar', 'default');
  await page.locator('[data-date="2026-10-27"]').click();
  assert.match(await text('#detail'), /퇴근 누락/); assert.match(await text('#detail'), /미산출/); assert.match(await text('#detail'), /퇴근 18:02 추가[\s\S]*대기/);
  await cap('calendar', 'day-detail');
  await page.locator('#prev').click(); assert.match(await text('#month'), /9월/); assert.equal(await page.locator('#cal button').count(), 30);
  await open('calendar', '?emp=A');
  await page.locator('[data-date="2026-10-27"]').click();
  assert.equal(await page.locator('#dayEdit').getAttribute('href'), 'obys-attendance-admin-attendance-edit.html?emp=A&date=2026-10-27');
  assert.equal(await page.locator('#toDetail').getAttribute('href'), 'obys-attendance-admin-employee-detail.html?emp=A');
  await page.locator('#dayEdit').click(); await page.waitForURL(/attendance-edit\.html\?emp=A&date=2026-10-27/);
  assert.equal(await page.locator('#date').inputValue(), '2026-10-27'); assert.match(await text('#cmpActual'), /퇴근 누락/);
  links.push('H->M');
  await open('calendar', '?emp=C&date=2026-10-15');
  assert.match(await text('#detail'), /반려/);
  checks.push('H:month-grid', 'H:day-detail-correction', 'H:month-nav', 'H:deeplink-from-B', 'H:day-edit->M');
  await common('calendar', '?emp=A');

  // ---- I 통계
  await open('stats');
  assert.equal(await page.locator('#kpis .metric').count(), 5);
  assert.match(await text('#kpis'), /정책 미정 — 설정 필요/);
  assert.equal(await page.locator('#chBranch svg rect').count(), 5);
  assert.equal(await page.locator('#chDow svg rect').count(), 7);
  assert.equal(await page.locator('#chHour svg rect').count(), 5);
  assert.match(await text('#payroll'), /좌표 미설정 지점[\s\S]*3곳/);
  await cap('stats', 'default');
  await page.locator('#branch').selectOption('b2'); await page.locator('#period').selectOption('week');
  assert.equal(await page.locator('#chBranch svg rect').count(), 1);
  await cap('stats', 'branch-week');
  checks.push('I:5-kpis', 'I:3-svg-charts', 'I:policy-undecided', 'I:payroll-summary');
  await common('stats');

  // ---- J 직원 목록 (명부·필터·일괄 선택 → L, 행 → K)
  await open('roster');
  const rRows = viewport === 'desktop' ? '#rows tr' : '#cards .rcard';
  assert.match(await text('#k0'), /8명/); assert.match(await text('#k1'), /1명/); assert.match(await text('#k2'), /1명/); assert.match(await text('#k3'), /4명/);
  assert.equal(await page.locator(rRows).count(), 9);
  assert.equal(await page.locator(rRows + '[data-state="pending"] a').count(), 0);
  assert.match(await text(rRows + '[data-state="pending"]'), /가입 승인 화면에서 처리/);
  await cap('roster', 'default');
  await page.locator('#state').selectOption('left'); assert.equal(await page.locator(rRows).count(), 1);
  await page.locator('#state').selectOption('all'); await page.locator('#branch').selectOption('b1'); assert.equal(await page.locator(rRows).count(), 4);
  await page.locator('#branch').selectOption(''); await page.locator('#state').selectOption('nosched'); assert.equal(await page.locator(rRows).count(), 4);
  await cap('roster', 'filtered');
  await page.locator('#q').fill('없는이름'); assert.match(await text('#none'), /검색 결과 없음/);
  await cap('roster', 'search-empty');
  await page.locator('#reset2').click(); assert.equal(await page.locator(rRows).count(), 9);
  await page.locator('#pickUnsched').click();
  assert.equal(await hid('#bulk'), false); assert.match(await text('#bulkTxt'), /^4명 선택/);
  assert.equal(await page.locator('#bulkGo').getAttribute('href'), 'obys-attendance-admin-schedule.html?emp=E,F,G,H');
  await cap('roster', 'bulk-selected');
  await page.locator('#bulkGo').click(); await page.waitForURL(/schedule\.html\?emp=E,F,G,H/);
  assert.match(await text('#pickSum'), /^4명 선택/); links.push('J-bulk->L');
  await open('roster');
  await page.locator(rRows + '[data-emp="E"] a').first().click(); await page.waitForURL(/employee-detail\.html\?emp=E/);
  assert.match(await text('h1'), /샘플 직원 E/); links.push('J->K');
  checks.push('J:kpi-active8-pending1-left1-nosched4', 'J:pending-no-link', 'J:status-branch-filter', 'J:search-empty', 'J:bulk-unscheduled->L', 'J:row->K');
  await common('roster');

  // ---- K 직원 상세 (탭 4개, K→H, K→L)
  await open('detail', '?emp=A');
  assert.equal(await page.locator('[role=tab]').count(), 4);
  assert.equal(await page.locator('#t-summary').getAttribute('aria-selected'), 'true');
  assert.equal(await page.locator('#sumM .metric').count(), 7);
  await cap('detail', 'default');
  await page.locator('#t-contract').click();
  assert.match(await text('#kv'), /월~금[\s\S]*09:00~18:00[\s\S]*40시간/); assert.match(await text('#pane'), /현재 데이터 있음 — 계약서 문자열/);
  await cap('detail', 'contract');
  await page.locator('#mkSched').click(); await page.waitForURL(/schedule\.html\?emp=A&src=contract/);
  assert.match(await text('#loadMsg'), /불러옴[\s\S]*샘플 직원 A 계약서/); links.push('K->L');
  await open('detail', '?emp=D&tab=schedule');
  assert.equal(await page.locator('#weekT tbody tr').count(), 7); assert.equal(await page.locator('#schedHist li').count(), 2);
  await cap('detail', 'schedule');
  await page.locator('#t-contract').click(); assert.match(await text('#diffWarn'), /계약서 근무시간\(10:00~19:00\)과 다릅니다/);
  await cap('detail', 'contract-schedule-diff');
  await open('detail', '?emp=A&tab=history');
  assert.match(await text('#histT'), /결근[\s\S]*샘플 관리자/); assert.match(await text('#pane'), /정정 요청 이력/);
  await cap('detail', 'history');
  await open('detail', '?emp=E');
  assert.match(await text('#noSchedWarn'), /결근을 판정할 수 없습니다/); assert.match(await text('#sumM'), /판정 불가/);
  await cap('detail', 'no-schedule');
  await page.locator('#t-contract').click(); assert.match(await text('#unsignedWarn'), /서명되지 않았습니다/);
  await open('detail', '?emp=H&tab=contract'); assert.match(await text('#noContract'), /근로계약서가 없습니다/);
  await cap('detail', 'no-contract');
  await open('detail', '?emp=A');
  await page.locator('#toCal').click(); await page.waitForURL(/employee-calendar\.html\?emp=A/);
  assert.equal(await page.locator('#emp').inputValue(), 'A'); links.push('K->H');
  checks.push('K:4-tabs', 'K:summary-7-metrics', 'K:contract-readonly-strings', 'K:contract->L-prefill', 'K:schedule-week+history', 'K:contract-vs-schedule-diff', 'K:attendance-history', 'K:no-schedule-absent-undecidable', 'K:unsigned-contract', 'K:no-contract', 'K:calendar->H');
  await common('detail', '?emp=A&tab=summary');

  // ---- L 근무 일정 설정
  await open('schedule');
  assert.match(await text('#loadMsg'), /현재 일정을 불러왔습니다/); assert.equal(await text('#weekH'), '40시간 0분');
  await cap('schedule', 'default');
  await open('schedule', '?emp=E,F,G,H');
  assert.match(await text('#loadMsg'), /근무 일정이 없습니다/); assert.equal(await text('#weekH'), '0시간 0분');
  await page.locator('#loadContract').click();
  assert.match(await text('#loadMsg'), /샘플 직원 E 계약서[\s\S]*미서명 계약서 값/); assert.equal(await text('#weekH'), '40시간 0분');
  assert.equal(await page.locator('#hoursWarn li[data-warn="days"]').count(), 2); assert.equal(await page.locator('#hoursWarn li[data-warn="none"]').count(), 1);
  await cap('schedule', 'contract-loaded');
  await page.locator('[data-on="6"]').check();
  assert.equal(await text('#weekH'), '48시간 0분');
  assert.equal(await page.locator('#hoursWarn li[data-warn="hours"]').count(), 3); assert.match(await text('#hoursWarn'), /저장은 막지 않습니다/);
  await cap('schedule', 'weekly-hours-warning');
  await page.locator('#from').fill('2026-09-28'); assert.match(await text('#fromMsg'), /마감된 기간/);
  await cap('schedule', 'start-date-locked');
  await page.locator('#from').fill('2026-11-02');
  await page.locator('#exDate').fill('2026-11-07'); await page.locator('#exType').selectOption('swap'); await page.locator('#exAdd').click();
  assert.match(await text('#exMsg'), /출근·퇴근 시각이 필요/);
  await page.locator('#exIn').fill('10:00'); await page.locator('#exOut').fill('14:00'); await page.locator('#exMemo').fill('토요일 행사 지원'); await page.locator('#exAdd').click();
  assert.match(await text('#exMsg'), /추가됨[\s\S]*4명/); assert.equal(await page.locator('#excRows tr').count(), 4);
  await page.locator('#hDate').fill('2026-11-20'); await page.locator('#hBr').selectOption('b3'); await page.locator('#hName').fill('샘플 3호점 내부 공사'); await page.locator('#hAdd').click();
  assert.equal(await page.locator('#holRows tr').count(), 3);
  await cap('schedule', 'exception-added');
  await page.locator('#save').click();
  assert.equal(await page.locator('#confirm').evaluate(d => d.open), true);
  assert.match(await text('#cfList'), /대상 4명[\s\S]*월·화·수·목·금·토[\s\S]*2026-11-02/); assert.match(await text('#cfWarn'), /계약서와 다른 점 7건/);
  await cap('schedule', 'save-confirm');
  await page.locator('#cfOk').click();
  assert.equal(await page.locator('#confirm').evaluate(d => d.open), false);
  assert.match(await text('#result'), /저장 완료[\s\S]*4명/); assert.match(await text('#toast'), /저장 완료/);
  await cap('schedule', 'saved');
  checks.push('L:load-current', 'L:multi-select-4', 'L:contract-load', 'L:weekly-hours-warning-not-blocking', 'L:start-date-closed-period', 'L:exception-validation', 'L:branch-holiday', 'L:save-confirm-dialog', 'L:saved');
  await common('schedule');

  // ---- M 출근/결근 개별 처리
  await open('edit');
  assert.match(await text('#cmpPlan'), /09:00[\s\S]*18:00/); assert.match(await text('#cmpActual'), /퇴근 누락/);
  assert.match(await text('#corrNote'), /정정 요청 대기 1건/); assert.equal(await page.locator('label.chip').count(), 9);
  await cap('edit', 'default');
  await page.locator('#saveOne').click(); assert.match(await text('#formErr'), /상태를 고르세요[\s\S]*5자 이상/);
  await cap('edit', 'reason-required');
  await page.locator('label.chip', {hasText: '정상'}).click(); await page.locator('#tout').fill('18:02'); await page.locator('#reason').fill('퇴근 버튼 누락');
  await page.locator('#saveOne').click();
  assert.match(await text('#logT'), /퇴근 누락 → 정상[\s\S]*출근 08:56 · 퇴근 없음 → 퇴근 18:02/); assert.match(await text('#orig'), /원래 값\(보존\): 출근 08:56 · 퇴근 없음/);
  await cap('edit', 'saved');
  await page.locator('label.chip', {hasText: '연차'}).click(); assert.equal(await hid('#payNote'), false);
  await open('edit', '?emp=F&date=2026-10-28');
  assert.match(await text('#cmpPlan'), /근무 일정 미등록/); assert.match(await text('#cmpActual'), /출근 기록 없음/);
  await page.locator('label.chip', {hasText: '지각'}).click(); await page.locator('#reason').fill('출근 버튼 오류, 점장 확인');
  await page.locator('#saveOne').click(); assert.match(await text('#formErr'), /출근 시각이 필요/);
  await page.locator('#tin').fill('10:20'); await page.locator('#saveOne').click(); assert.match(await text('#logT'), /미출근 → 지각[\s\S]*출근 10:20/);
  await cap('edit', 'manual-clock-in');
  await open('edit', '?emp=D&date=2026-09-24');
  assert.match(await text('#locked'), /마감된 기간 — 수정할 수 없습니다/); assert.equal(await page.locator('#fs').evaluate(f => f.disabled), true);
  assert.match(await text('#logT'), /미출근 → 결근/);
  await cap('edit', 'period-locked');
  await open('edit');
  await page.locator('#bPreview').click();
  assert.match(await text('#bPrev'), /결근 확정 대상 1건 — 샘플 직원 B 10-13[\s\S]*일정 미등록 10건[\s\S]*출근 기록 있음 4건/);
  assert.equal(await page.locator('#bApply').isDisabled(), false);
  await cap('edit', 'bulk-preview');
  await page.locator('#bApply').click(); assert.match(await text('#bErr'), /5자 이상/);
  await page.locator('#bReason').fill('연락 없이 출근하지 않음 — 점장 확인'); await page.locator('#bApply').click();
  assert.match(await text('#bDone'), /1건 결근 확정/);
  await page.locator('#bPreview').click(); assert.match(await text('#bPrev'), /대상 0건[\s\S]*이미 처리됨 1건/);
  await cap('edit', 'bulk-done');
  await page.locator('#bAct').selectOption('undo'); await page.locator('#bPreview').click(); assert.match(await text('#bPrev'), /결근 확정 해제 대상 1건 — 샘플 직원 B 10-13/);
  checks.push('M:plan-vs-actual', 'M:9-statuses', 'M:reason-required', 'M:original-preserved+history', 'M:manual-clock-in', 'M:closed-period-locked', 'M:bulk-preview-exclusions', 'M:bulk-apply+undo', 'M:paid-leave-policy-undecided');
  await common('edit', '?emp=A&date=2026-10-27');

  assert.deepEqual(errors, []); assert.deepEqual(requests, []); assert.deepEqual(overflow, []); assert.deepEqual(small, []);
  assert.deepEqual([...links].sort(), ['G->K', 'H->M', 'J->K', 'J-bulk->L', 'K->H', 'K->L']);
  results.push({viewport, width, height, passed: true, console_errors: errors, network_requests: requests, horizontal_overflow: overflow, small_targets_under_44px: small, link_checks: links, capture_count: captures.length, captures, checks});
  await ctx.close();
 }
 } finally { await browser.close(); }
 fs.writeFileSync(path.join(root, 'evidence', 'playwright-results.json'), JSON.stringify({tool: 'playwright-core', bundle: 'obys-attendance-v4-admin', results}, null, 2) + '\n');
 console.log(JSON.stringify(results.map(r => ({viewport: r.viewport, passed: r.passed, captures: r.capture_count, errors: r.console_errors.length, requests: r.network_requests.length, overflow: r.horizontal_overflow.length, small: r.small_targets_under_44px.length, links: r.link_checks.length, checks: r.checks.length}))));
})().catch(e => { console.error(e); process.exitCode = 1; });

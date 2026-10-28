// v3 관리자 근태 관리 목업 로컬 검증. 서비스/API 에 접속하지 않는다(file:// 만 연다).
// 사용: node assets/verify.cjs  → evidence/*.png, evidence/playwright-results.json
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const SCREENS = {
 onboarding: 'obys-attendance-admin-onboarding', today: 'obys-attendance-admin-today', branches: 'obys-attendance-admin-branches',
 employees: 'obys-attendance-admin-employees', calendar: 'obys-attendance-admin-employee-calendar', stats: 'obys-attendance-admin-stats'
};
const STATES = ['loading', 'empty', 'error', 'permission', 'offline'];
const TAP = 'button,a[href],select,input:not([type=checkbox]):not([type=hidden]),textarea,summary,label.check';
(async () => {
 const browser = await chromium.launch({executablePath: process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell', headless: true, args: ['--no-sandbox']});
 const results = [];
 try {
 for (const [viewport, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
  const ctx = await browser.newContext({viewport: {width, height}});
  const page = await ctx.newPage();
  const errors = [], requests = [], captures = [], overflow = [], small = [], checks = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('request', r => { if (/^https?:/.test(r.url())) requests.push(r.url()); });
  const open = async (key, q = '') => { await page.goto('file://' + path.join(root, SCREENS[key] + '.html') + q); };
  const text = sel => page.locator(sel).first().innerText();
  const hid = sel => page.locator(sel).first().evaluate(e => e.hidden);
  const cap = async (key, state) => {
   const over = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
   if (over > 0) overflow.push({screen: key, state, over});
   const tiny = await page.evaluate(sel => [...document.querySelectorAll(sel)].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden' && (r.height < 43.5 || r.width < 43.5); }).map(e => `${e.tagName}#${e.id || ''}:${(e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 20)}:${Math.round(e.getBoundingClientRect().width)}x${Math.round(e.getBoundingClientRect().height)}`), TAP);
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
   assert.equal(navs.length, 7);
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
  await page.waitForURL(/employee-calendar\.html\?emp=B/);
  assert.equal(await page.locator('#emp').inputValue(), 'B');
  checks.push('G:8-employees', 'G:branch-filter-sort', 'G:search-empty', 'G:date-range-error', 'G:row->H');
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
  await open('calendar', '?emp=C&date=2026-10-15');
  assert.match(await text('#detail'), /반려/);
  checks.push('H:month-grid', 'H:day-detail-correction', 'H:month-nav', 'H:deeplink-from-B');
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

  assert.deepEqual(errors, []); assert.deepEqual(requests, []); assert.deepEqual(overflow, []); assert.deepEqual(small, []);
  results.push({viewport, width, height, passed: true, console_errors: errors, network_requests: requests, horizontal_overflow: overflow, small_targets_under_44px: small, capture_count: captures.length, captures, checks});
  await ctx.close();
 }
 } finally { await browser.close(); }
 fs.writeFileSync(path.join(root, 'evidence', 'playwright-results.json'), JSON.stringify({tool: 'playwright-core', bundle: 'obys-attendance-v3-admin', results}, null, 2) + '\n');
 console.log(JSON.stringify(results.map(r => ({viewport: r.viewport, passed: r.passed, captures: r.capture_count, errors: r.console_errors.length, requests: r.network_requests.length, overflow: r.horizontal_overflow.length, small: r.small_targets_under_44px.length, checks: r.checks.length}))));
})().catch(e => { console.error(e); process.exitCode = 1; });

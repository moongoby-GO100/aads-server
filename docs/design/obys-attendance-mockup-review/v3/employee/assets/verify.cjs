// v3 직원 근태 앱 목업 로컬 검증. 서비스/API 에 접속하지 않는다(file:// 만 연다).
// 사용: node assets/verify.cjs  → evidence/*.png, evidence/playwright-results.json
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const SCREENS = {home: 'obys-attendance-employee-home', clock: 'obys-attendance-employee-clock', records: 'obys-attendance-employee-records'};
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
  const open = async (key, h = '') => { await page.goto('file://' + path.join(root, SCREENS[key] + '.html') + h); };
  const text = sel => page.locator(sel).first().innerText();
  const hid = sel => page.locator(sel).first().evaluate(e => e.hidden);
  const vis = sel => page.locator(sel).first().isVisible();
  const cap = async (key, state) => {
   const over = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
   if (over > 0) overflow.push({screen: key, state, over});
   const tiny = await page.evaluate(sel => [...document.querySelectorAll(sel)].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden' && (r.height < 43.5 || r.width < 43.5); }).map(e => `${e.tagName}#${e.id || ''}:${(e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 20)}:${Math.round(e.getBoundingClientRect().width)}x${Math.round(e.getBoundingClientRect().height)}`), TAP);
   small.push(...tiny.map(t => ({screen: key, state, target: t})));
   const file = `${key}-${viewport}-${state}.png`;
   await page.screenshot({path: path.join(root, 'evidence', file), fullPage: true});
   captures.push({screen_id: SCREENS[key], state, file});
  };
  const common = async key => {
   await open(key);
   for (const state of STATES) {
    await page.locator('#scenario').selectOption(state);
    assert.equal(await hid('#live'), true); assert.equal(await hid('#stateBox'), false);
    assert.equal(await page.locator('#recover').count(), 1);
    await cap(key, state);
    await page.locator('#recover').click();
    assert.equal(await hid('#live'), false); assert.equal(await hid('#stateBox'), true);
   }
   await page.locator('#scenario').selectOption('session-expired');
   await page.waitForURL(/\/login\.html\?return=/);
   assert.match(await text('h1'), /^로그인$/);
   await page.locator('#login').click();
   await page.waitForURL(u => u.pathname.endsWith(SCREENS[key] + '.html'));
   const tabs = await page.locator('nav.tabs a').evaluateAll(es => es.map(e => e.getAttribute('href')));
   assert.equal(tabs.length, 4);
   for (const h of tabs) assert.ok(fs.existsSync(path.resolve(root, h.split('#')[0])), h);
   checks.push(key + ':five-states+session-redirect+tabs');
  };

  // ---- E 홈
  await open('home');
  assert.match(await text('#hState'), /출근 전/);
  assert.equal(await text('#mainBtn'), '출근');
  const btn = await page.locator('#mainBtn').boundingBox(); assert.ok(btn.height >= 72);
  assert.equal(await page.locator('nav.tabs a').count(), 4);
  await cap('home', 'default');
  await page.locator('#mainBtn').click(); assert.match(await text('#hState'), /근무 중/); assert.equal(await text('#mainBtn'), '퇴근');
  await cap('home', 'working');
  await page.locator('#mainBtn').click(); assert.match(await text('#hSub'), /8시간 5분/);
  await page.locator('a[href="obys-attendance-employee-records.html#correction"]').first().click();
  await page.waitForURL(/records\.html#correction/); assert.equal(await hid('#fm'), false);
  checks.push('E:status-card+big-button', 'E:shortcuts', 'E:bottom-tabs', 'E:alert->D-correction');
  await common('home');

  // ---- C 출퇴근
  await open('clock');
  assert.equal(await text('#clockBtn'), '출근');
  await cap('clock', 'default');
  await page.locator('#showConsent').click(); assert.ok(await vis('#consentView'));
  await cap('clock', 'consent');
  await page.locator('#decline').click(); assert.ok(await vis('#declinedView'));
  await cap('clock', 'denied');
  await page.locator('#clockBtn').click(); assert.match(await text('#verdict'), /확인필요 · 위치 미확인/);
  await page.locator('#reset').click();
  await page.locator('#branchSel').selectOption('b3'); assert.ok(await vis('#coordView'));
  await cap('clock', 'coord-unset');
  await page.locator('#clockBtn').click(); assert.match(await text('#verdict'), /좌표 미설정 지점/);
  await page.locator('#reset').click();
  await page.locator('#perm').selectOption('off'); await page.locator('#clockBtn').click();
  assert.ok(await vis('#permView'));
  await page.locator('#retryLoc').click(); assert.match(await text('#permMsg'), /꺼져 있습니다/);
  await cap('clock', 'permission-off');
  await page.locator('#perm').selectOption('on'); await page.locator('#retryLoc').click();
  assert.match(await text('#verdict'), /반경 안/); assert.match(await text('#elapsed'), /근무 중/);
  await cap('clock', 'working');
  await page.locator('#breakStart').click(); assert.match(await text('#todayStatus'), /휴게 중/); assert.equal(await page.locator('#clockBtn').isDisabled(), true);
  await cap('clock', 'break');
  await page.locator('#breakEnd').click(); await page.locator('#clockBtn').click();
  assert.ok(await vis('#doneView')); assert.match(await text('#doneTxt'), /휴게 30분 · 근무 8시간 35분/);
  await cap('clock', 'done');
  checks.push('C:consent', 'C:denied-saves-review', 'C:coord-unset', 'C:permission-off-recovery', 'C:working-break-done');
  await common('clock');

  // ---- D 내 기록
  await open('records');
  assert.match(await text('#notice'), /퇴근 기록이 없는 날이 2건[\s\S]*10-27/);
  await cap('records', 'default');
  await page.locator('#tabC').click(); assert.equal(await page.locator('#cal button').count(), 31);
  const missBefore = await page.locator('#cal button.c-missing').count();
  assert.equal(await page.locator('#cal button.c-missing[data-day="2026-10-27"]').count(), 1);
  await page.locator('[data-day="2026-10-27"]').click(); assert.match(await text('#dayInfo'), /누락/);
  await cap('records', 'calendar');
  await page.locator('#nGo').click(); await page.locator('#fmOk').click(); assert.match(await text('#fmErr'), /5자 이상/);
  await page.locator('#fmReason').fill('퇴근 버튼을 누르지 못하고 퇴근했습니다.');
  await cap('records', 'missing-clock-out');
  await page.locator('#fmOk').click(); assert.match(await text('#notice'), /처리 대기/);
  await cap('records', 'correction-submitted');
  await page.locator('#resNo').click(); assert.match(await text('#notice'), /반려되었습니다/);
  await cap('records', 'correction-rejected');
  await page.locator('#again').click(); await page.locator('#fmReason').fill('점장 확인을 받았습니다. 18시 2분 퇴근입니다.'); await page.locator('#fmOk').click();
  await page.locator('#resOk').click(); assert.match(await text('#notice'), /승인되었습니다[\s\S]*퇴근 18:02/);
  assert.equal(await page.locator('#cal button.c-missing').count(), missBefore - 1);
  await cap('records', 'correction-approved');
  checks.push('D:month-calendar', 'D:missing->request', 'D:reason-5', 'D:pending', 'D:rejected->again', 'D:approved-result');
  await common('records');

  assert.deepEqual(errors, []); assert.deepEqual(requests, []); assert.deepEqual(overflow, []); assert.deepEqual(small, []);
  results.push({viewport, width, height, passed: true, console_errors: errors, network_requests: requests, horizontal_overflow: overflow, small_targets_under_44px: small, capture_count: captures.length, captures, checks});
  await ctx.close();
 }
 } finally { await browser.close(); }
 fs.writeFileSync(path.join(root, 'evidence', 'playwright-results.json'), JSON.stringify({tool: 'playwright-core', bundle: 'obys-attendance-v3-employee', results}, null, 2) + '\n');
 console.log(JSON.stringify(results.map(r => ({viewport: r.viewport, passed: r.passed, captures: r.capture_count, errors: r.console_errors.length, requests: r.network_requests.length, overflow: r.horizontal_overflow.length, small: r.small_targets_under_44px.length, checks: r.checks.length}))));
})().catch(e => { console.error(e); process.exitCode = 1; });

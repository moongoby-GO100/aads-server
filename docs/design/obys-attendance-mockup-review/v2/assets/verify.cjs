// v2 목업 로컬 검증. 서비스/API 에 접속하지 않는다(file:// 만 연다).
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const FILE = {
 onboarding: 'obys-attendance-admin-onboarding.html',
 today: 'obys-attendance-admin-today.html',
 clock: 'obys-attendance-employee-clock.html',
 records: 'obys-attendance-employee-records.html'
};
const SCREEN_ID = { onboarding: 'obys-attendance-admin-onboarding', today: 'obys-attendance-admin-today', clock: 'obys-attendance-employee-clock', records: 'obys-attendance-employee-records' };
const TAP = 'button,a[href],select,input:not([type=checkbox]):not([type=hidden]),textarea,summary,label.check';
(async () => {
 const browser = await chromium.launch({executablePath:process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',headless:true,args:['--no-sandbox']});
 const results = [];
 try {
 for (const [viewport, width, height] of [['desktop',1440,1000],['mobile',390,844]]) {
  const ctx = await browser.newContext({viewport:{width,height}});
  const page = await ctx.newPage();
  const errors = [], requests = [], captures = [], overflow = [], small = [];
  page.on('pageerror', e=>errors.push(e.message));
  page.on('console', m=>{if(m.type()==='error') errors.push(m.text())});
  page.on('request', r=>{if(/^https?:/.test(r.url())) requests.push(r.url())});
  const open = async key => { await page.goto('file://'+path.join(root,FILE[key])); };
  const cap = async (key, state) => {
   const over = await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
   if (over > 0) overflow.push({screen:key,state,over});
   const tiny = await page.evaluate(sel=>[...document.querySelectorAll(sel)].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'&&(r.height<43.5||r.width<43.5)}).map(e=>`${e.tagName}#${e.id||''}:${(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,20)}:${Math.round(e.getBoundingClientRect().width)}x${Math.round(e.getBoundingClientRect().height)}`), TAP);
   small.push(...tiny.map(t=>({screen:key,state,target:t})));
   const file = `${key}-${viewport}-${state}.png`;
   await page.screenshot({path:path.join(root,'evidence',file),fullPage:true});
   captures.push({screen_id:SCREEN_ID[key],state,file});
  };
  const text = sel => page.locator(sel).first().innerText();
  const vis = async sel => (await page.locator(sel).filter({visible:true}).count());
  const checks = [];

  // ---- A 관리자 시작하기
  await open('onboarding');
  assert.equal(await page.locator('.steps li').count(), 3);
  assert.match(await text('#warn'), /좌표 미설정 지점 7곳/);
  assert.match(await text('#s1'), /기본 50m · 상한 50m/);
  assert.match(await text('#mCoord'), /0 \/ 7/);
  assert.equal(await text('#mRec'), '0건');
  assert.equal(await vis('.tag.now'), (await page.locator('.tag.now').count()));
  await cap('onboarding','default');
  await page.locator('#toggleUnset').click();
  assert.equal(await page.locator('#unsetRows tr').count(), 7);
  assert.equal(await page.locator('#unsetRows .badge').count(), 7);
  await cap('onboarding','coordinates-unset');
  await page.locator('#toggleUnset').click();
  await page.locator('#openRecs').click();
  assert.match(await text('#recs'), /아직 근태 기록이 없습니다/);
  await cap('onboarding','empty');
  await page.locator('#go1').click(); assert.match(await text('#toast'), /attendance-admin\.html/);
  await page.locator('#copy').click(); assert.match(await text('#toast'), /복사/);
  await page.locator('#demoSet').click(); assert.match(await text('#mCoord'), /1 \/ 7/); assert.match(await text('#warn'), /6곳/);
  checks.push('A:checklist-3-steps','A:coordinates-unset-7','A:radius-50m','A:empty');

  // ---- B 관리자 오늘 현황 (새 컨텍스트 저장소 = 가상 시드)
  await open('today');
  const metric = async k => (await page.locator('.metric').filter({hasText:k}).first().innerText()).replace(/\s+/g,' ');
  assert.match(await metric('출근'), /4명/); assert.match(await metric('미출근'), /3명/); assert.match(await metric('근무 중'), /3명/); assert.match(await metric('확인필요'), /4건/);
  assert.equal(await page.locator('#inbox .item').count(), 4);
  const inboxText = await text('#inbox');
  for (const reason of ['위치 미확인','퇴근 누락','좌표 미설정 지점']) assert.match(inboxText, new RegExp(reason));
  assert.match(await text('#corrN'), /대기 1건/);
  await cap('today','default');
  await page.locator('button[data-id="r5"]').click();
  assert.match(await text('#detail'), /퇴근 누락/); assert.match(await text('#detail'), /미산출/);
  await page.locator('#approve').click(); assert.match(await text('#toast'), /운영 반영 없음/);
  await page.locator('#modify').click(); await page.locator('#pause').fill('30'); await page.locator('#save').click();
  assert.match(await text('#toast'), /원본 유지/);
  await cap('today','detail');
  await page.locator('#search').fill('존재하지않는샘플');
  assert.match(await text('#rows'), /검색결과 없음/);
  await cap('today','search-empty');
  await page.locator('#reset').click();
  await page.locator('#from').fill('2026-10-29');
  assert.match(await text('#rows'), /기간 오류/);
  await page.locator('#reset').click();
  await page.locator('#branch').selectOption('br-sc'); assert.equal(await page.locator('.record').count(), 1);
  await page.locator('#reset').click();
  await page.locator('#closeBtn').click();
  assert.equal(await page.locator('#closeOk').isDisabled(), true);
  assert.match(await text('#closeTxt'), /확인필요 4건/);
  await cap('today','period-close');
  await page.locator('#closeCk').check(); assert.equal(await page.locator('#closeOk').isDisabled(), false);
  await page.locator('#closeOk').click(); assert.match(await text('#toast'), /급여 반영 확인 완료/);
  const baseRows = await page.locator('.record').count();
  for (const state of ['loading','empty','error','session-expired','permission','offline']) {
   await page.locator('#scenario').selectOption(state);
   assert.equal(await vis('.record'), 0);
   assert.equal(await vis('#inbox .item'), 0);
   await cap('today',state);
   await page.locator('#recover').click();
   assert.equal(await page.locator('.record').count(), baseRows);
  }
  checks.push('B:summary-4','B:inbox-3-reasons','B:detail-approve-edit-demo','B:search-empty','B:date-range-error','B:period-close-confirm','B:recovery-six-states');

  // ---- C 직원 출퇴근
  await open('clock');
  assert.equal(await vis('#consentView'), 1);
  await cap('clock','consent');
  await page.locator('#agree').click();
  assert.equal(await text('#clockBtn'), '출근');
  await cap('clock','default');
  await page.locator('#branchSel').selectOption('br-ys');
  assert.match(await text('#coordView'), /위치 확인 불가 — 관리자 확인 후 확정/);
  await cap('clock','coord-unset');
  await page.locator('#branchSel').selectOption('br-gn');
  await page.locator('#perm').selectOption('off');
  await page.locator('#clockBtn').click();
  assert.equal(await vis('#permView'), 1);
  await cap('clock','permission-off');
  await page.locator('#retryLoc').click(); assert.equal(await vis('#permView'), 1);
  await page.locator('#reset').click();
  assert.equal(await vis('#consentView'), 1);
  await page.locator('#decline').click();
  assert.equal(await vis('#declinedView'), 1); assert.match(await text('#declinedView'), /확인필요로 남습니다/);
  await cap('clock','denied');
  await page.locator('#clockBtn').click();
  assert.match(await text('#elapsed'), /3시간 12분 근무 중/);
  assert.match(await text('#verdict'), /확인필요 · 위치 미확인/);
  await cap('clock','working');
  await page.locator('#breakStart').click();
  assert.match(await text('#todayStatus'), /휴게 중/); assert.equal(await vis('#breakEnd'), 1);
  await cap('clock','break');
  await page.locator('#breakEnd').click();
  assert.match(await text('#elapsed'), /3시간 42분/);
  await page.locator('#clockBtn').click();
  assert.equal(await vis('#doneView'), 1); assert.match(await text('#doneTxt'), /8시간 33분/);
  await cap('clock','done');
  checks.push('C:consent-denied-notice','C:permission-off-recovery','C:coord-unset-notice','C:working-break-done','C:denied-checkin-saved');

  // ---- 여정 접점 1: C 위치 거부 출근 -> B 대기함 '위치 미확인'
  await open('today');
  const aItem = page.locator('#inbox .item[data-rec="r-a-today"]');
  assert.equal(await aItem.count(), 1);
  assert.match(await aItem.innerText(), /샘플 직원 A[\s\S]*위치 미확인/);
  assert.match(await metric('출근'), /5명/);
  checks.push('JOURNEY1:C-denied->B-inbox');

  // ---- D 직원 내 기록: 정정 요청 제출
  await open('records');
  assert.match(await text('#notice'), /퇴근 기록이 없는 날이 1건/);
  assert.equal(await page.locator('.rec').count(), 3);
  await cap('records','default');
  await page.locator('#tabM').click(); assert.ok(await page.locator('.rec').count() >= 7); await page.locator('#tabW').click();
  await page.locator('#openFm').click();
  await page.locator('#fmOk').click(); assert.match(await text('#fmErr'), /5자 이상/);
  await page.locator('#fmReason').fill('퇴근 버튼을 누르지 못하고 퇴근했습니다.');
  await cap('records','missing-clock-out');
  await page.locator('#fmOk').click();
  assert.match(await text('#notice'), /처리 대기/); assert.match(await text('#list'), /정정 요청 처리 대기/);
  await cap('records','correction-submitted');

  // ---- 여정 접점 2: D 정정 요청 -> B 대기, 반려
  await open('today');
  assert.match(await text('#corrN'), /대기 2건/);
  assert.equal(await page.locator('#corr [data-corr="c2"]').count(), 1);
  await page.locator('[data-rj="c2"]').click();
  await page.locator('#rejOk').click(); assert.match(await text('#rejErr'), /5자 이상/);
  await page.locator('#rejReason').fill('출근만 기록되어 있어 퇴근 시각을 확인할 수 없습니다. 점장 확인 후 다시 요청해 주세요.');
  await cap('today','correction-reject');
  await page.locator('#rejOk').click();
  assert.match(await text('#corr [data-corr="c2"]'), /반려됨[\s\S]*점장 확인/);
  checks.push('JOURNEY2:D-request->B-queue','B:correction-reject-reason-required');

  // ---- 접점 3: B 반려 -> D 결과, 다시 요청
  await open('records');
  assert.match(await text('#notice'), /반려되었습니다[\s\S]*점장 확인/);
  await cap('records','correction-rejected');
  await page.locator('#again').click();
  await page.locator('#fmReason').fill('점장에게 확인받았습니다. 18시에 퇴근했습니다.');
  await page.locator('#fmOk').click();
  assert.match(await text('#notice'), /처리 대기/);

  // ---- 접점 4: B 승인 -> D 결과
  await open('today');
  await page.locator('[data-ap="c3"]').click();
  assert.match(await text('#toast'), /정정 승인 완료/);
  assert.match(await text('#corr [data-corr="c3"]'), /승인됨/);
  assert.equal(await page.locator('#inbox .item[data-rec="r6"]').count(), 0);
  await cap('today','correction-approve');
  await open('records');
  assert.match(await text('#notice'), /승인되었습니다[\s\S]*9시간 0분/);
  assert.match(await text('#sumW'), /25시간 33분/);
  await cap('records','correction-result');
  checks.push('JOURNEY3:B-reject->D-reason','JOURNEY4:B-approve->D-result');

  assert.deepEqual(errors, []); assert.deepEqual(requests, []); assert.deepEqual(overflow, []); assert.deepEqual(small, []);
  results.push({viewport,width,height,passed:true,console_errors:errors,network_requests:requests,horizontal_overflow:overflow,small_targets_under_44px:small,capture_count:captures.length,captures,checks});
  await ctx.close();
 }
 } finally {await browser.close()}
 fs.writeFileSync(path.join(root,'evidence','playwright-results.json'),JSON.stringify({tool:'playwright-core',results},null,2));
 console.log(JSON.stringify(results.map(r=>({viewport:r.viewport,passed:r.passed,captures:r.capture_count,errors:r.console_errors.length,requests:r.network_requests.length,overflow:r.horizontal_overflow.length,small:r.small_targets_under_44px.length}))));
})().catch(e=>{console.error(e);process.exitCode=1});

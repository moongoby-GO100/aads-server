// 변경 전 재구성 캡처(v4 admin). J·K·L·M 은 신규 화면 — 현재 없음, 진입점 admin.html#attendance 를 캡처한다. origin/main 의 app/static/apps/obys 화면(admin.html#attendance · attendance-admin.html · clock.html)을
// 가상 API 응답으로 렌더한다. 실 API·외부 접속 없음 — mock.local 외 요청은 모두 abort.
// 사용: node assets/before-reconstruct.cjs   (묶음 폴더는 이 파일 위치로 정한다: admin | employee)
const { chromium } = require('playwright-core');
const fs = require('fs'), path = require('path');
const BUNDLE = path.basename(path.resolve(__dirname, '..'));
const STATIC = process.env.OBYS_STATIC || path.resolve(__dirname, '../../../../../../app/static');
const OUT = path.resolve(__dirname, '../before');
const FIXED_NOW = Date.parse('2026-10-28T03:14:00Z');
const T = '2026-10-28';

// ---- 가상 지점 5곳(주소 있음 2 · 좌표 없음 5) — 목업 v4 와 같은 이름
const BRN = {b1: '샘플 1호점', b2: '샘플 2호점', b3: '샘플 3호점', b4: '샘플 4호점', b5: '샘플 5호점'};
const EMP_BR = {A: 'b1', B: 'b1', C: 'b1', D: 'b2', E: 'b2', F: 'b3', G: 'b4', H: 'b5'};
const R = (id, emp, date, i, o, brk, st, memo) => ({id, status: st, display: {'근태번호': id, '일자': date, '지점': BRN[EMP_BR[emp]], '직원': '샘플 직원 ' + emp}, raw: {start_at: i, end_at: o, break_minutes: brk, status: st, memo}});
// virtual-v4-sample: 오늘 6명 출근(2명 미출근) · 확인필요(위치 미확인/좌표 미설정 지점/퇴근 누락)
const SAMPLE = [
 R('r1', 'A', T, '08:58', null, 0, 'approved', ''), R('r2', 'B', T, '08:51', null, 0, 'approved', ''), R('r3', 'C', T, '09:12', null, 0, 'approved', ''),
 R('r4', 'D', T, '08:55', null, 0, 'approved', ''), R('r5', 'E', T, '09:03', null, 0, 'pending', '위치 미확인'), R('r6', 'H', T, '09:20', null, 0, 'pending', '좌표 미설정 지점'),
 R('r7', 'A', '2026-10-27', '08:56', null, 0, 'pending', '퇴근 누락'), R('r8', 'B', '2026-10-27', '08:50', '18:04', 60, 'approved', ''),
 R('r9', 'D', '2026-10-23', '09:24', '18:02', 60, 'approved', ''), R('r10', 'F', '2026-10-27', '08:57', '18:01', 60, 'pending', '좌표 미설정 지점')];
const geofence = {policy: {min_radius_m: 30, max_radius_m: 50, default_radius_m: 50}, branches: Object.entries(BRN).map(([id, name]) => ({id, name, latitude: null, longitude: null, geofence_radius_m: null}))};
const clockBase = {server_time: '2026-10-28T03:14:00Z', employee: {name: '샘플 직원 A'}, business: {name: '샘플 사업자'},
 branches: [{id: 'b1', name: BRN.b1, has_geofence: true}, {id: 'b3', name: BRN.b3, has_geofence: false}], consent: {agreed: true}, open_record: null, today: []};

// screen: 매니페스트 screen_id 의 짧은 이름, fixture: 목업 기본 캡처와 같은 fixture_id
const JOBS = BUNDLE === 'admin' ? [
 {screen: 'onboarding', url: '/static/apps/obys/admin.html#attendance', records: [], fixture: 'virtual-v4-empty'},
 {screen: 'today', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-sample'},
 {screen: 'employees', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-sample'},
 {screen: 'calendar', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-sample'},
 {screen: 'stats', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-sample'},
 {screen: 'branches', url: '/static/apps/obys/attendance-admin.html', records: [], fixture: 'virtual-v4-branches'},
 {screen: 'roster', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-roster'},
 {screen: 'detail', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-roster'},
 {screen: 'schedule', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-roster'},
 {screen: 'edit', url: '/static/apps/obys/admin.html#attendance', records: SAMPLE, fixture: 'virtual-v4-roster'}
] : [
 {screen: 'home', url: '/static/apps/obys/clock.html', fixture: 'virtual-v4-employee-a'},
 {screen: 'clock', url: '/static/apps/obys/clock.html', fixture: 'virtual-v4-employee-a'},
 {screen: 'records', url: '/static/apps/obys/clock.html', fixture: 'virtual-v4-employee-a'}
];
const unhandled = new Set();
(async () => {
 fs.mkdirSync(OUT, {recursive: true});
 const browser = await chromium.launch({executablePath: process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell', headless: true, args: ['--no-sandbox']});
 const out = [];
 try {
 for (const job of JOBS) for (const [vp, w, h] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
  const ctx = await browser.newContext({viewport: {width: w, height: h}, serviceWorkers: 'block', timezoneId: 'Asia/Seoul', locale: 'ko-KR'});
  await ctx.addInitScript(ts => {
   const RD = Date;
   window.Date = class extends RD { constructor(...a) { if (a.length) super(...a); else super(ts); } static now() { return ts; } };
   localStorage.setItem('aads_token', 'sample-not-a-token'); localStorage.setItem('obys_v41_business_id', 'biz-sample');
  }, FIXED_NOW);
  await ctx.route('**/*', async route => {
   const u = new URL(route.request().url());
   if (u.host !== 'mock.local') return route.abort();
   if (u.pathname.startsWith('/static/')) { const f = path.join(STATIC, u.pathname.slice('/static/'.length)); return fs.existsSync(f) && fs.statSync(f).isFile() ? route.fulfill({path: f}) : route.fulfill({status: 404, body: ''}); }
   const j = b => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(b)});
   const p = u.pathname;
   if (p.endsWith('/auth/tenants')) return j({tenants: [{tenant_id: 't-sample', name: '샘플 회사'}], current_tenant_id: 't-sample'});
   if (p.endsWith('/auth/me')) return j({tenant_id: 't-sample', user: {id: 'u-sample', name: '샘플 관리자', email: 'sample@example.invalid'}, membership: {tenant_id: 't-sample', role: 'owner', status: 'active'}});
   if (p.endsWith('/businesses')) return j({businesses: [{id: 'biz-sample', name: '샘플 사업자', business_name: '샘플 사업자'}]});
   if (p.endsWith('/attendance/clock/state')) return j(clockBase);
   if (p.endsWith('/branches/geofence')) return j(geofence);
   if (p.endsWith('/attendance/locations')) return j({records: [], count: 0, summary: {inside: 0, outside: 0, unknown: 0, pending: 0}});
   if (p.includes('/attendance/records')) return j({records: job.records || [], source: {name: 'obys.yeoljeong_attendance_records', live: true}});
   unhandled.add(p); return j({});
  });
  const page = await ctx.newPage();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://mock.local' + job.url);
  await page.waitForTimeout(2500);
  const file = path.join(OUT, `before-${job.screen}-${vp}-default.png`);
  await page.screenshot({path: file, fullPage: true});
  out.push({screen: job.screen, vp, fixture: job.fixture, url: job.url, file: path.basename(file), errors, text: (await page.locator('body').innerText()).slice(0, 160).replace(/\s+/g, ' ')});
  await ctx.close();
 }
 } finally { await browser.close(); }
 fs.writeFileSync(path.join(OUT, 'before-jobs.json'), JSON.stringify({bundle: BUNDLE, static_root: 'app/static (origin/main)', jobs: out.map(({text, ...o}) => o), unhandled: [...unhandled]}, null, 1) + '\n');
 console.log(JSON.stringify({out, unhandled: [...unhandled]}, null, 1));
})().catch(e => { console.error(e); process.exitCode = 1; });

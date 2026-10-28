// 변경 전(현 origin/main app/static/apps/obys 의 admin.html#attendance · clock.html) 재구성 캡처. 실 API 접속 없음 — 모든 /api 는 가상 샘플(virtual-7)로 응답.
// 사용: node before-reconstruct.cjs <출력디렉터리>  (STATIC 은 저장소 app/static, 기본값은 이 파일 기준 상대경로)
const { chromium } = require('playwright-core');
const fs = require('fs'), path = require('path');
const STATIC = process.env.OBYS_STATIC || path.resolve(__dirname, '../../../../../app/static');
const OUT = process.argv[2] || path.resolve(__dirname, '../before');
const FIXED_NOW = Date.parse('2026-10-28T03:14:00Z');

// ---- virtual-7: v2 목업(verify.cjs)의 seed 와 같은 가상 직원 A~G · 지점 3곳
const BRN = {'br-gn':'샘플 강남점','br-sc':'샘플 서초점','br-ys':'샘플 역삼점'};
const T = '2026-10-28';
const R = (id,emp,br,date,i,o,brk,geo,st) => ({id,emp,br,date,i,o,brk,geo,st});
const seed = [R('r1','B','br-gn',T,'09:00',null,0,'inside','approved'),R('r2','C','br-sc',T,'09:10',null,0,'unknown','pending'),R('r3','D','br-gn',T,'08:00','17:00',60,'inside','approved'),R('r4','E','br-gn',T,'09:30',null,0,'unknown','pending'),R('r5','F','br-gn','2026-10-27','09:00',null,0,'inside','pending'),R('r6','A','br-gn','2026-10-27','09:00',null,0,'inside','pending'),R('r7','B','br-gn','2026-10-26','09:00','18:00',60,'inside','approved'),R('r8','D','br-gn','2026-10-26','09:00','18:00',60,'inside','approved'),R('r9','A','br-gn','2026-10-26','09:00','18:00',60,'inside','approved')];
['19','20','21','22','23'].forEach(d => seed.push(R('ra'+d,'A','br-gn','2026-10-'+d,'09:00','18:00',60,'inside','approved')));
const memo = r => r.st !== 'pending' ? '' : (!r.o && r.date < T) ? '퇴근 누락' : r.geo === 'unknown' ? (r.br === 'br-gn' ? '위치 미확인' : '좌표 미설정 지점') : '';
const adminRec = r => ({id:r.id,status:r.st,display:{'근태번호':r.id,'일자':r.date,'지점':BRN[r.br],'직원':'샘플 직원 '+r.emp},raw:{start_at:r.i,end_at:r.o,break_minutes:r.brk,status:r.st,memo:memo(r)}});

const branches = Object.entries(BRN).map(([id,name]) => ({id,name,has_geofence:id==='br-gn'}));
const base = {server_time:'2026-10-28T03:14:00Z',employee:{name:'샘플 직원 A'},business:{name:'샘플 사업자'},branches};
const clockStates = {
 default: {...base,consent:{agreed:false},open_record:null,today:[]},
 working: {...base,consent:{agreed:true},open_record:{id:'att-a',check_in_at:'2026-10-28T00:02:00Z',status:'pending',geofence_result:'unknown',memo:'위치 확인 불가'},today:[]}
};

// screen: 화면 키, url: 열 주소, records: admin 근태 기록(null 이면 admin 아님), clock: clockStates 키
const JOBS = [
 {screen:'onboarding', url:'/static/apps/obys/admin.html#attendance', records:[], mode:'default'},
 {screen:'today', url:'/static/apps/obys/admin.html#attendance', records:seed.map(adminRec), mode:'default'},
 {screen:'clock', url:'/static/apps/obys/clock.html', clock:'default', mode:'default'},
 {screen:'clock', url:'/static/apps/obys/clock.html', clock:'working', mode:'working'},
 {screen:'records', url:'/static/apps/obys/clock.html', clock:'default', mode:'default'}
];
const unhandled = new Set();
(async () => {
 const browser = await chromium.launch({executablePath:process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',headless:true,args:['--no-sandbox']});
 const out = [];
 for (const job of JOBS) for (const [vp,w,h] of [['desktop',1440,1000],['mobile',390,844]]) {
  const ctx = await browser.newContext({viewport:{width:w,height:h},serviceWorkers:'block',timezoneId:'Asia/Seoul',locale:'ko-KR'});
  await ctx.addInitScript(ts => {
   const RD = Date;
   window.Date = class extends RD { constructor(...a) { if (a.length) super(...a); else super(ts); } static now() { return ts; } };
   localStorage.setItem('aads_token','sample-not-a-token'); localStorage.setItem('obys_v41_business_id','biz-sample');
  }, FIXED_NOW);
  await ctx.route('**/*', async route => {
   const u = new URL(route.request().url());
   if (u.host !== 'mock.local') return route.abort();
   if (u.pathname.startsWith('/static/')) { const f = path.join(STATIC, u.pathname.slice('/static/'.length)); return fs.existsSync(f)&&fs.statSync(f).isFile() ? route.fulfill({path:f}) : route.fulfill({status:404,body:''}); }
   const j = b => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(b)});
   const p = u.pathname;
   if (p.endsWith('/auth/tenants')) return j({tenants:[{tenant_id:'t-sample',name:'샘플 회사'}],current_tenant_id:'t-sample'});
   if (p.endsWith('/auth/me')) return j({tenant_id:'t-sample',user:{id:'u-sample',name:'샘플 관리자',email:'sample@example.invalid'},membership:{tenant_id:'t-sample',role:'owner',status:'active'}});
   if (p.endsWith('/businesses')) return j({businesses:[{id:'biz-sample',name:'샘플 사업자',business_name:'샘플 사업자'}]});
   if (p.endsWith('/attendance/clock/state')) return j(clockStates[job.clock]);
   if (p.includes('/attendance/records')) return j({records:job.records||[],source:{name:'obys.yeoljeong_attendance_records',live:true}});
   unhandled.add(p); return j({});
  });
  const page = await ctx.newPage();
  const errors = []; page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://mock.local'+job.url);
  await page.waitForTimeout(2500);
  const stem = job.screen === 'clock' ? `before-clock-${vp}-${job.mode}` : `before-${job.screen}-${vp}-${job.mode}`;
  const file = path.join(OUT, stem + '.png');
  await page.screenshot({path:file,fullPage:true});
  out.push({screen:job.screen,vp,mode:job.mode,file:path.basename(file),errors,text:(await page.locator('body').innerText()).slice(0,300).replace(/\s+/g,' ')});
  await ctx.close();
 }
 await browser.close();
 console.log(JSON.stringify({out,unhandled:[...unhandled]},null,1));
})().catch(e=>{console.error(e);process.exitCode=1});

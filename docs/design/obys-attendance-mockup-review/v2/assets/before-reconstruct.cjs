// 변경 전(현 origin/main app/static/apps/obys/clock.html) 재구성 캡처. 실 API 접속 없음 — 모든 /api 는 가상 샘플로 응답.
// 사용: node before-reconstruct.cjs <출력디렉터리>  (STATIC 은 저장소 app/static, 기본값은 이 파일 기준 상대경로)
const { chromium } = require('playwright-core');
const fs = require('fs'), path = require('path');
const STATIC = process.env.OBYS_STATIC || path.resolve(__dirname, '../../../../../app/static');
const OUT = process.argv[2] || path.resolve(__dirname, '../before');
const FIXED_NOW = Date.parse('2026-10-28T03:14:00Z');
const branches = [{id:'br-gn',name:'샘플 강남점',has_geofence:true},{id:'br-sc',name:'샘플 서초점',has_geofence:false},{id:'br-ys',name:'샘플 역삼점',has_geofence:false}];
const base = {server_time:'2026-10-28T03:14:00Z',employee:{name:'샘플 직원 A'},business:{name:'샘플 사업자'},branches};
const states = {
 default: {...base,consent:{agreed:false},open_record:null,today:[]},
 working: {...base,consent:{agreed:true},open_record:{id:'att-a',check_in_at:'2026-10-28T00:02:00Z',status:'pending',geofence_result:'unknown',memo:'위치 확인 불가'},today:[]}
};
const unhandled = new Set();
(async () => {
 const browser = await chromium.launch({executablePath:process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',headless:true,args:['--no-sandbox']});
 const out = [];
 for (const mode of Object.keys(states)) for (const [vp,w,h] of [['desktop',1440,1000],['mobile',390,844]]) {
  const ctx = await browser.newContext({viewport:{width:w,height:h},serviceWorkers:'block',timezoneId:'Asia/Seoul',locale:'ko-KR'});
  await ctx.addInitScript(ts=>{Date.now=()=>ts;localStorage.setItem('aads_token','sample-not-a-token')},FIXED_NOW);
  await ctx.route('**/*', async route => {
   const u = new URL(route.request().url());
   if (u.host !== 'mock.local') return route.abort();
   if (u.pathname.startsWith('/static/')) { const f = path.join(STATIC, u.pathname.slice('/static/'.length)); return fs.existsSync(f)&&fs.statSync(f).isFile() ? route.fulfill({path:f}) : route.fulfill({status:404,body:''}); }
   const j = b => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(b)});
   const p = u.pathname;
   if (p.endsWith('/businesses')) return j({businesses:[{id:'biz-sample',name:'샘플 사업자'}]});
   if (p.endsWith('/attendance/clock/state')) return j(states[mode]);
   unhandled.add(p); return j({});
  });
  const page = await ctx.newPage();
  const errors = []; page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://mock.local/static/apps/obys/clock.html');
  await page.waitForTimeout(1500);
  const file = path.join(OUT, `before-clock-${vp}-${mode}.png`);
  await page.screenshot({path:file,fullPage:true});
  out.push({vp,mode,file:path.basename(file),errors,text:(await page.locator('body').innerText()).slice(0,300).replace(/\s+/g,' ')});
  await ctx.close();
 }
 await browser.close();
 console.log(JSON.stringify({out,unhandled:[...unhandled]},null,1));
})().catch(e=>{console.error(e);process.exitCode=1});

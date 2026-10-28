// 변경 전(현 origin/main admin.html#attendance) 재구성 캡처. 실 API 접속 없음 — 모든 /api 는 가상 샘플로 응답.
const { chromium } = require('playwright-core');
const fs = require('fs'), path = require('path');
const STATIC = '/tmp/obys-att-recover/static/app/static';
const OUT = process.argv[2];
const mode = process.argv[3] || 'default';
const recs = [
 {id:'att-a',status:'approved',display:{"근태번호":"att-a","일자":"2026-10-26","지점":"샘플 강남점","직원":"샘플 직원 A"},raw:{start_at:'09:00',end_at:'18:00',break_minutes:60,status:'approved',memo:''}},
 {id:'att-b',status:'pending',display:{"근태번호":"att-b","일자":"2026-10-26","지점":"샘플 서초점","직원":"샘플 직원 B"},raw:{start_at:'10:00',end_at:null,break_minutes:0,status:'pending',memo:'퇴근 누락'}},
 {id:'att-c',status:'pending',display:{"근태번호":"att-c","일자":"2026-10-26","지점":"샘플 서초점","직원":"샘플 직원 C"},raw:{start_at:'22:00',end_at:'06:00',break_minutes:60,status:'pending',memo:'좌표 미설정'}},
 {id:'att-d',status:'rejected',display:{"근태번호":"att-d","일자":"2026-10-25","지점":"샘플 강남점","직원":"샘플 직원 D"},raw:{start_at:'09:00',end_at:'13:00',break_minutes:0,status:'rejected',memo:''}}];
const unhandled = [], seen = [];
(async () => {
 const browser = await chromium.launch({executablePath:'/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',headless:true,args:['--no-sandbox']});
 const out = [];
 for (const [vp,w,h] of [['desktop',1440,1000],['mobile',390,844]]) {
  const ctx = await browser.newContext({viewport:{width:w,height:h}});
  await ctx.addInitScript(()=>{localStorage.setItem('aads_token','sample-not-a-token');localStorage.setItem('obys_v41_business_id','biz-sample')});
  await ctx.route('**/*', async route => {
   const u = new URL(route.request().url());
   if (u.host !== 'mock.local') return route.abort();
   if (u.pathname.startsWith('/static/')) { const f = path.join(STATIC, u.pathname.slice('/static/'.length)); return fs.existsSync(f)&&fs.statSync(f).isFile() ? route.fulfill({path:f}) : route.fulfill({status:404,body:''}); }
   const j = b => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(b)});
   const p = u.pathname; seen.push(p);
   if (p.endsWith('/auth/tenants')) return j({tenants:[{tenant_id:'t-sample',name:'샘플 회사'}],current_tenant_id:'t-sample'});
   if (p.endsWith('/auth/me')) return j({tenant_id:'t-sample',user:{id:'u-sample',name:'샘플 관리자',email:'sample@example.invalid'},membership:{tenant_id:'t-sample',role:'owner',status:'active'}});
   if (p.includes('/businesses')) return j({businesses:[{id:'biz-sample',name:'샘플 사업자',business_name:'샘플 사업자'}]});
   if (p.includes('/attendance/records')) return mode==='empty' ? j({records:[],source:{name:'obys.yeoljeong_attendance_records',live:true}}) : j({records:recs,source:{name:'obys.yeoljeong_attendance_records',live:true}});
   unhandled.push(p); return j({});
  });
  const page = await ctx.newPage();
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://mock.local/static/apps/obys/admin.html#attendance');
  await page.waitForTimeout(2500);
  const file = path.join(OUT, `before-${vp}-${mode}.png`);
  await page.screenshot({path:file,fullPage:true});
  const text = (await page.locator('body').innerText()).slice(0,2500).replace(/\s+/g,' ');
  out.push({vp,file,errors,text});
  await ctx.close();
 }
 await browser.close();
 console.log(JSON.stringify({out,unhandled:[...new Set(unhandled)],seen:[...new Set(seen)]},null,1));
})().catch(e=>{console.error(e);process.exitCode=1});

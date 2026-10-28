// 로컬 승인용 목업 검증. 서비스/API에 접속하지 않는다.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
 const browser = await chromium.launch({executablePath:process.env.MOCKUP_CHROMIUM || '/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',headless:true,args:['--no-sandbox']});
 const results = [];
 try {
 for (const [viewport, width, height] of [['desktop',1440,1000],['mobile',390,844]]) {
  const page = await browser.newPage({viewport:{width,height}});
  const errors = [], requests = [];
  page.on('pageerror', e=>errors.push(e.message));
  page.on('console', m=>{if(m.type()==='error') errors.push(m.text())});
  page.on('request', r=>{if(/^https?:/.test(r.url())) requests.push(r.url())});
  await page.goto('file://'+path.join(root,'오비서-근태-현황-목업-obys-attendance-mockup.html'));
  const capture = async state => {
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), 'horizontal overflow: '+state);
   await page.screenshot({path:path.join(root,'evidence',`${viewport}-${state}.png`),fullPage:true});
  };
  assert.equal(await page.locator('.record').count(),4);
  await capture('default');
  await page.getByRole('button',{name:'샘플 직원 B 상세',exact:true}).click();
  assert.match(await page.locator('#detail').innerText(),/퇴근 누락|퇴근 누락/);
  assert.match(await page.locator('#detail').innerText(),/미산출/);
  await page.locator('#approve').click();
  assert.match(await page.locator('#toast').innerText(),/운영 반영 없음/);
  await page.locator('#modify').click();
  await page.locator('#pause').fill('30');
  await page.locator('#save').click();
  assert.match(await page.locator('#toast').innerText(),/원본 유지/);
  await capture('detail');
  await page.locator('#search').fill('존재하지않는샘플');
  assert.match(await page.locator('#rows').innerText(),/검색결과 없음/);
  await capture('search-empty');
  await page.locator('#reset').click();
  await page.locator('#branch').selectOption('샘플 서초점');
  assert.equal(await page.locator('.record').count(),2);
  await page.locator('#status').selectOption('pending');
  assert.equal(await page.locator('.record').count(),1);
  assert.match(await page.locator('#rows').innerText(),/좌표 미설정/);
  await capture('coordinates-unset');
  await page.locator('#reset').click();
  await page.locator('#from').fill('2026-10-27');
  assert.match(await page.locator('#rows').innerText(),/기간 오류/);
  await page.locator('#reset').click();
  for(const state of ['loading','empty','error','session-expired','permission','offline']) {
   await page.locator('#scenario').selectOption(state);
   assert.equal(await page.locator('.record:visible').count(),0);
   await capture(state);
   await page.locator('#recover').click();
   assert.equal(await page.locator('.record:visible').count(),4);
  }
  await page.locator('summary').click();
  page.once('dialog',d=>d.accept());
  await page.locator('#coords').click();
  const formula = await page.evaluate(()=>[minutes({start:'22:00',end:'06:00',pause:60}),minutes({start:'09:00',end:'09:00',pause:0}),minutes({start:'09:00',end:null,pause:0}),minutes({start:'09:00',end:'10:00',pause:61})]);
  assert.deepEqual(formula,[420,null,null,null]);
  assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
  results.push({viewport,width,height,passed:true,console_errors:errors,network_requests:requests,captures:10,checks:['filters','date-range-validation','detail','missing-clock-out','coordinates-unset','approve-demo','edit-demo','empty-vs-search-empty','recovery-six-states','auxiliary-location','formula','no-overflow']});
  await page.close();
 }
 } finally {await browser.close()}
 fs.writeFileSync(path.join(root,'evidence','playwright-results.json'),JSON.stringify({tool:'playwright-core',results},null,2));
 console.log(JSON.stringify(results));
})().catch(e=>{console.error(e);process.exitCode=1});

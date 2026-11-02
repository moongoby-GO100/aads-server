/* Isolated UI fixture. All API results are in-memory; never an operational E2E. */
const {chromium} = require('playwright-core');
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const evidence=process.env.AADS_E2E_EVIDENCE_DIR || '/tmp/obys-correction-ui-evidence';
fs.mkdirSync(evidence,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
 let checks=0;
 try {for(const width of [1920,390]) {
  const page=await browser.newPage({viewport:{width,height:900}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const admin=fs.readFileSync(path.join(root,'app/static/apps/obys/admin.html'),'utf8');
  const css=[...admin.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m=>m[1]).join('\n');
  await page.setContent(`<html lang="ko"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}body{display:block}#host{max-width:100%;padding:16px}textarea{max-width:100%}</style><section id="host" class="section"></section></html>`);
  await page.addScriptTag({path:path.join(root,'app/static/apps/obys/modules/attendance-corrections.js')});
  await page.evaluate(()=>{
   window.fixture={items:[],writes:[],mode:'ok',reads:0,record:{id:'r/1',work_date:'2026-11-01',start_at:'09:00:00',end_at:'18:00:00',break_minutes:60}};
   const f=window.fixture;
   window.fixtureApi=async(url,options={})=>{
    if(!options.method) {
     f.reads++;
     if(f.mode==='read403') throw Object.assign(new Error('계정 확인'),{status:403});
     if(url.endsWith('/clock/state')) return {recent:[f.record]};
     return {items:structuredClone(f.items)};
    }
    const payload=JSON.parse(options.body);f.writes.push({url,payload});
    await new Promise(resolve=>setTimeout(resolve,40));
    if(['403','409','401'].includes(f.mode)) throw Object.assign(new Error(f.mode==='409'?'마감 근태 변경은 급여 정정 버전이 필요합니다':'계정 확인'),{status:Number(f.mode)});
    let row;
    if(url.includes('/records/')) {
     assertPayload(payload);
     if(f.items.some(row=>row.status==='pending')) throw Object.assign(new Error('처리 대기 중'),{status:409});
     row={id:'c'+(f.items.length+1),record_id:f.record.id,status:'pending',version:1,employee_email:'me@example.test',original:structuredClone(f.record),requested:payload,reason:payload.reason,supersedes_id:payload.supersedes_id};
     f.items.unshift(row);
    } else {
     row=f.items.find(row=>url.includes('/'+row.id+'/'));
     if(payload.version!==row.version) throw new Error('version contract');
     row.status=url.endsWith('/approve')?'approved':url.endsWith('/reject')?'rejected':'withdrawn';row.version++;row.decision_reason=payload.reason;
    }
    if(f.mode==='lost') throw new Error('network response lost after save');
    if(f.mode==='malformed') return {};
    return structuredClone(row);
   };
   function assertPayload(p) {if('record_id' in p || typeof p.break_minutes!=='number') throw new Error('create contract');}
   window.mountFixture=manager=>window.ObysAttendanceCorrections.mount(document.querySelector('#host'),{api:window.fixtureApi,businessId:'fixture-business',manager});
  });
  const mount=manager=>page.evaluate(manager=>window.mountFixture(manager),manager);
  const ready=()=>page.waitForFunction(()=>document.querySelector('[data-message]').textContent.includes('최신 상태를 확인했습니다'));
  const capture=name=>page.screenshot({path:path.join(evidence,`${width}-${name}.png`),fullPage:true});
  const mode=value=>page.evaluate(value=>{window.fixture.mode=value},value);
  const create=async()=>{
   await page.locator('[name=record_id]').selectOption('r/1');
   await page.locator('[name=work_date]').fill('2026-11-02');
   await page.locator('[name=break_minutes]').fill('0');
   await page.locator('[name=reason]').fill('실제 출근 정정 <script>');
   await page.locator('form button').click();await ready();
  };
  await mount(false);await ready();await create();
  assert.equal(await page.locator('[data-action=withdraw]').count(),1);checks++;
  assert.match(await page.locator('[data-list]').innerText(),/2026-11-01 \(일\).*|2026-11-02 \(월\)/);checks++;
  await capture('employee-pending');
  await page.locator('[data-reason]').fill('철회 후 다시 신청');
  await page.locator('[data-action=withdraw]').click();await ready();
  await page.locator('[data-action=reapply]').click();await page.locator('form button').click();await ready();
  assert.equal(await page.evaluate(()=>window.fixture.writes.at(-1).payload.supersedes_id),'c1');checks++;
  await mount(true);await ready();
  await page.locator('[data-id=c2] [data-reason]').fill('시간 재확인');
  await page.locator('[data-id=c2] [data-action=reject]').click();await ready();await capture('admin-rejected');
  await mount(false);await ready();await page.locator('[data-id=c2] [data-action=reapply]').click();await page.locator('form button').click();await ready();
  await mount(true);await ready();
  await page.locator('[data-id=c3] [data-reason]').fill('원본과 확인 완료');
  const before=await page.evaluate(()=>window.fixture.writes.length);
  await page.locator('[data-id=c3] [data-action=approve]').evaluate(button=>{button.click();button.click()});await ready();
  assert.equal(await page.evaluate(()=>window.fixture.writes.length),before+1);checks++;
  assert.match(await page.locator('[data-id=c3]').innerText(),/승인/);await capture('admin-approved');
  // Each error leaves writes blocked until an explicit read; reconnect never resends.
  for(const state of ['403','409','401','lost','malformed']) {
   await page.evaluate(()=>{window.fixture.items=[]});await mode('ok');await mount(false);await ready();
   await page.locator('[name=record_id]').selectOption('r/1');await page.locator('[name=reason]').fill('통신 오류 검증');
   await mode(state);await page.locator('form button').click();
   await page.waitForFunction(()=>document.querySelector('[data-message]').textContent.match(/권한이 없습니다|충돌|세션이 만료|처리 결과를 확인/));
   assert.equal(await page.locator('form button').isDisabled(),true);checks++;
   const count=await page.evaluate(()=>window.fixture.writes.length);
   await page.evaluate(()=>window.dispatchEvent(new Event('online')));
   assert.equal(await page.evaluate(()=>window.fixture.writes.length),count);checks++;
   await capture('recovery-'+state);
   await mode('ok');await page.locator('[data-refresh]').click();await ready();
   assert.equal(await page.evaluate(()=>window.fixture.writes.length),count);checks++;
   if(['lost','malformed'].includes(state)) assert.equal(await page.locator('[data-action=withdraw]').count(),1);
  }
  await mode('read403');await page.locator('[data-refresh]').click();
  await page.waitForFunction(()=>document.querySelector('[data-message]').textContent.includes('권한이 없습니다'));
  assert.equal(await page.locator('[data-login]').isVisible(),true);checks++;
  assert.deepEqual(errors,[]);checks++;
  await page.close();
 }
 // Source wiring is checked in addition to executing the actual renderer/handlers above.
 const clock=fs.readFileSync(path.join(root,'app/static/apps/obys/clock.html'),'utf8');
 const admin=fs.readFileSync(path.join(root,'app/static/apps/obys/admin.html'),'utf8');
 for(const source of [clock,admin]) {assert.match(source,/attendance-corrections.js/);assert.match(source,/ObysAttendanceCorrections.mount/);}
 console.log(JSON.stringify({result:'PASS',checks,fixture:true,widths:[1920,390],evidence}));
 }finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});

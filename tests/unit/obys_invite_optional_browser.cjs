// Isolated browser fixture: no operational accounts or API writes.
const {chromium}=require('playwright-core');
const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
 try {
  const html=fs.readFileSync('app/static/apps/obys/admin.html','utf8');
  const source=html.slice(html.indexOf('    function renderEmployeeOnboardingV41()'),html.indexOf('    async function loadJoinRequestsV41'));
  assert(source.length>0);
  const page=await browser.newPage({viewport:{width:1100,height:900}});
  await page.setContent('<main id="page"><div class="hero"></div></main>');
  await page.evaluate(source=>{
   window.writes=[];window.copies=[];window.toasts=[];
   window.page=document.querySelector('#page');
   window.escapeHtml=s=>String(s).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
   window.showToast=s=>toasts.push(s);window.authHeaders=()=>({});
   window.financeApiV41=async(url,opts)=>{
    if(opts?.method==='POST'){writes.push(JSON.parse(opts.body));return {invite:{token:'fixture-token',branch_labels:['테스트 매장']}}}
    if(url.includes('tenant-registry'))return {businesses:[{id:'fixture-business',name:'테스트 매장'}]};
    return {invites:[]};
   };
   window.INVITE_ROLE_LABEL_V41={};
   window.loadJoinRequestsV41=async()=>{};
   eval(source+';window.renderFixture=renderEmployeeOnboardingV41');
   Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{if(window.copyFails)throw Error('denied');copies.push(value)}}});
  },source);
  await page.evaluate(()=>renderFixture());
  const create=async email=>{await page.locator('[name=email]').fill(email);await page.locator('[name=invite_target]').check();await page.locator('#empInviteV41 button').click()};
  await create('');await page.locator('#empInviteCopyV41').waitFor();
  assert.equal(await page.evaluate(()=>writes[0].email),'');
  assert(!/email|undefined/.test(await page.locator('#empInviteLinkV41').inputValue()));
  assert.equal(await page.evaluate(()=>copies.length),0);
  await page.locator('#empInviteCopyV41').click();assert.equal(await page.evaluate(()=>copies.length),1);
  await page.evaluate(()=>window.copyFails=true);await page.locator('#empInviteCopyV41').click();
  assert.match(await page.locator('#empInviteCopyStatusV41').innerText(),/Ctrl\+C/);
  assert.equal(await page.locator('#empInviteLinkV41').evaluate(e=>e.selectionEnd-e.selectionStart),await page.locator('#empInviteLinkV41').evaluate(e=>e.value.length));
  await page.screenshot({path:'/tmp/obys-invite-manual-copy.png',fullPage:true});
  await page.evaluate(()=>window.copyFails=false);await page.locator('#empInviteCopyV41').click();assert.equal(await page.evaluate(()=>copies.length),2);
  await create('bad');assert.equal(await page.evaluate(()=>writes.length),1);
  await create('Staff@example.com');assert.equal(await page.evaluate(()=>writes[1].email),'staff@example.com');
  assert.match(await page.locator('#empInviteLinkV41').inputValue(),/yf_invite_email=staff%40example.com/);
  await page.screenshot({path:'/tmp/obys-invite-email.png',fullPage:true});
  console.log('PASS: optional/valid/invalid email, explicit copy, retry, manual selection, URL hints; 2 screenshots');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

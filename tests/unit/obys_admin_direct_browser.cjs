/* Isolated browser fixtures: no production API calls, payroll confirmations or messages. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const admin=fs.readFileSync(path.join(root,'app/static/apps/obys/admin.html'),'utf8');
const css=[...admin.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m=>m[1]).join('\n');
const evidence=process.env.AADS_E2E_EVIDENCE_DIR;
if(evidence)fs.mkdirSync(evidence,{recursive:true});
const line=name=>admin.split('\n').find(line=>line.trim().startsWith(`function ${name}(`)||line.trim().startsWith(`async function ${name}(`)).trim();
(async()=>{
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox']});
try{for(const width of [1920,390]){
 const page=await browser.newPage({viewport:{width,height:width===390?844:911}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
 await page.setContent(`<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head><body><main id="page" class="page"></main></body></html>`);
 const names=['scheduleMonthRangeV41','scheduleCountV41','captureScheduleDraftV41','restoreScheduleDraftV41','scheduleNavigateV41','scheduleShiftV41','loadWorkSchedulesV41','workSchedulePanelV41','bindWorkSchedulesV41','maskEmployeeEmailV41'];
 await page.addScriptTag({content:`
 const state={businessId:'fixture-b',readRole:'admin',peopleMonth:'2026-11',scheduleWeek:'2026-11-01',scheduleView:'week',scheduleUnit:'week',scheduleEmployees:{'fixture-b':[{employee_email:'one@example.test',employee_name:'시험 직원',branch:'시험 지점'}]}};
 const page=document.getElementById('page'),configs={attendance:{data:[]}};
 window.fixtureState=state;window.fixturePaths=[];window.fixtureWrites=[];
 const escapeHtml=value=>String(value??'').replace(/[&<>"]/g,'');
 const kstDateV41=()=> '2026-11-02';
 const peopleMonthBoundsV41=()=>({month:state.peopleMonth,...scheduleMonthRangeV41(state.peopleMonth)});
 const workspaceApi=async(path,options={})=>{window.fixturePaths.push(path);if(options.method==='POST'){window.fixtureWrites.push(JSON.parse(options.body));return {count:1}}return {schedules:[{id:'fixture-schedule',employee_email:'one@example.test',employee_name:'시험 직원',work_date:'2026-11-30',kind:'work',start_time:'22:00',end_time:'06:00',break_minutes:0}],counts:{}}};
 const renderPageV41=()=>{page.innerHTML=workSchedulePanelV41(escapeHtml);bindWorkSchedulesV41()};
 const loadLiveRoute=async()=>{state.workSchedules=await loadWorkSchedulesV41(state.businessId,peopleMonthBoundsV41());renderPageV41()};
 const previewContractSchedulesV41=()=>{},openRosterEditorV41=()=>{};
 ${names.map(line).join('\n')}
 loadLiveRoute();`});
 await page.locator('input[name="employee"]').check();
 await page.locator('input[name="start_time"]').fill('22:00');
 await page.locator('input[name="end_time"]').fill('06:00');
 await page.locator('input[name="break_minutes"]').fill('0');
 await page.locator('#scheduleUnit').selectOption('month');
 await page.waitForFunction(()=>document.querySelector('#weeklyRoster thead tr').children.length===31);
 assert.equal(await page.locator('#scheduleView').inputValue(),'month');
 assert.equal(await page.locator('input[name="employee"]').isChecked(),true);
 assert.equal(await page.locator('input[name="start_time"]').inputValue(),'22:00');
 assert.equal(await page.locator('input[name="break_minutes"]').inputValue(),'0');
 assert.ok((await page.locator('#weeklyRoster').innerText()).includes('22:00–06:00'));
 if(evidence)await page.screenshot({path:`${evidence}/roster-${width}-month.png`,fullPage:true});
 await page.locator('#scheduleMonth').fill('2027-01');await page.locator('#scheduleMonth').dispatchEvent('change');
 await page.waitForFunction(()=>document.querySelector('#weeklyRoster thead tr').children.length===32);
 await page.locator('#scheduleUnit').selectOption('week');
 await page.waitForFunction(()=>document.querySelector('#weeklyRoster thead tr').children.length===8);
 await page.locator('#schedulePrevWeek').click();
 await page.waitForFunction(()=>document.querySelector('#scheduleWeek').value==='2026-12-25');
 await page.locator('#scheduleNextWeek').click();
 await page.waitForFunction(()=>document.querySelector('#scheduleWeek').value==='2027-01-01');
 await page.locator('#scheduleBulk button[type="submit"]').click();
 await page.waitForFunction(()=>window.fixtureWrites.length===1);
 const bulk=await page.evaluate(()=>window.fixtureWrites[0]);
 assert.equal(bulk.start_time,'22:00');assert.equal(bulk.break_minutes,0);assert.equal(bulk.employees[0].employee_email,'one@example.test');
 // The same server bulk route is reused when re-registering an existing roster.
 await page.locator('input[name="start_time"]').fill('21:00');
 await page.locator('#scheduleBulk button[type="submit"]').click();
 await page.waitForFunction(()=>window.fixtureWrites.length===2);
 assert.equal(await page.evaluate(()=>window.fixtureWrites[1].start_time),'21:00');
 const contractNames=['contractSuggestV41','contractFormHtmlV41','contractFormsHtmlV41','contractExistingKeysV41','contractFormReadV41','contractCellsV41','contractBulkPayloadV41'];
 const bindSource=admin.slice(admin.indexOf('    function bindContractFormsV41('),admin.indexOf('    async function previewContractSchedulesV41('));
 await page.addScriptTag({content:contractNames.map(line).join('\n')+'\n'+bindSource+`
 const showToast=()=>{};
 state.workSchedules={month:{rows:[{employee_email:'one@example.test',work_date:'2026-11-30'}]}};
 const contractTarget=document.createElement('div');contractTarget.id='fixture-contract';page.append(contractTarget);
 contractTarget.innerHTML=contractFormsHtmlV41([{employee_email:'one@example.test',employee_name:'시험 직원',suggested:{workdays:[0],default:{start_time:'22:00',end_time:'06:00',break_minutes:0}},missing:[]}],'2026-11-30','2026-11-30',false);
 bindContractFormsV41(contractTarget,state.businessId);`});
 await page.locator('[data-cf-save]').click();
 assert.equal(await page.evaluate(()=>window.fixtureWrites.length),2,'preserve option must not overwrite');
 await page.locator('input[name="cf_start"]').fill('20:00');
 await page.locator('[data-cf-update]').click();
 await page.waitForFunction(()=>window.fixtureWrites.length===3);
 assert.equal(await page.evaluate(()=>window.fixtureWrites[2].cells[0].start_time),'20:00');
 await page.setContent(`<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head><body><main class="page"><section class="section" id="payroll"></section></main></body></html>`);
 await page.addScriptTag({path:path.join(root,'app/static/apps/obys/modules/payroll-admin.js')});
 await page.evaluate(async()=>{
  window.payrollCalls=[];window.pdfCalls=[];window.savedCount=0;window.failSave=false;
  const base={business_id:'fixture-b',payroll_month:'2026-11',non_tax_meal_allowance:0,other_deduction:0,tax_withholding:100,insurance_deduction:200};
  const a={...base,id:'statement-a',employee_email:'a@example.test',gross_pay:10000,taxable_pay:10000,net_pay:9700,status:'draft',version:1};
  const b={...base,id:'statement-b',employee_email:'b@example.test',gross_pay:20000,taxable_pay:20000,net_pay:19700,status:'confirmed',version:2};
  window.originalConfirmed=JSON.stringify(b);
  const api=async(path,options={})=>{
   window.payrollCalls.push({path,options});
   if(path.startsWith('/employees/approved'))return {employees:[{id:'employee-a',email:'a@example.test',name:'시험 직원 A',branch:'지점 A'},{id:'employee-b',email:'b@example.test',name:'시험 직원 B',branch:'지점 B'}]};
   if(path.startsWith('/payroll?'))return {statements:[a,b,{...a,id:'other-business',business_id:'other'}]};
   if(path.startsWith('/payroll/defaults?'))return {defaults:{gross_pay:12000,taxable_pay:12000,non_tax_meal_allowance:0},basis:'서명 계약 기준'};
   if(path==='/payroll/draft-from-attendance')throw Object.assign(new Error('확정되지 않은 근태가 남아 있습니다'),{status:409});
   if(path==='/payroll'&&options.method==='POST'){
    if(window.failSave){window.failSave=false;throw new Error('fixture save failure')}
    const value=JSON.parse(options.body);window.lastSaved=value;
    const statement={...value,id:value.id==='statement-b'?'statement-b-v3':value.id||'new',version:value.id==='statement-b'?3:1,status:'draft',net_pay:value.gross_pay-value.tax_withholding-value.insurance_deduction-value.other_deduction};
    return {statement};
   }
   if(path.endsWith('/versions'))return {versions:[b,{...b,version:3,status:'draft',gross_pay:21000}]};
   throw new Error('Unexpected fixture API '+path);
  };
  await window.ObysPayrollAdmin.mount(document.getElementById('payroll'),{api,businessId:'fixture-b',month:'2026-11',onSaved:()=>{window.savedCount++},downloadPdf:id=>{window.pdfCalls.push(id)}});
 });
 const choose=page.locator('[data-payroll-employee]');
 assert.equal(await page.locator('input[name="gross_pay"]').inputValue(),'10000');
 assert.equal(await page.locator('[data-payroll-statement] option[value="other-business"]').count(),0);
 await choose.selectOption('b@example.test');
 assert.equal(await page.locator('input[name="gross_pay"]').inputValue(),'20000');
 assert.ok((await page.locator('[data-payroll-state]').innerText()).includes('확정'));
 await page.locator('input[name="gross_pay"]').fill('21000');await page.locator('input[name="taxable_pay"]').fill('21000');
 assert.equal(await page.locator('[data-payroll-action="confirm"]').isDisabled(),true);
 await page.evaluate(()=>{window.failSave=true});
 await page.locator('[data-payroll-form] button').click();
 await page.waitForFunction(()=>document.querySelector('[data-payroll-message]').textContent.includes('fixture save failure'));
 assert.equal(await page.locator('input[name="gross_pay"]').inputValue(),'21000');
 await page.locator('[data-payroll-form] button').click();await page.waitForFunction(()=>window.savedCount===1);
 const saved=await page.evaluate(()=>window.lastSaved);
 assert.equal(saved.employee_email,'b@example.test');assert.equal(saved.id,'statement-b');assert.equal(saved.business_id,'fixture-b');assert.equal(saved.payroll_month,'2026-11');assert.equal(saved.gross_pay,21000);
 assert.equal(await page.locator('[data-payroll-statement]').inputValue(),'statement-b-v3');
 assert.ok((await page.locator('[data-payroll-state]').innerText()).includes('버전 3'));
 await page.locator('[data-payroll-action="versions"]').click();await page.locator('[data-payroll-versions] h3').waitFor();
 assert.ok((await page.locator('[data-payroll-versions]').innerText()).includes('버전 2'));
 await page.locator('[data-payroll-action="pdf"]').click();await page.waitForFunction(()=>window.pdfCalls.length===1);
 await page.locator('[data-payroll-action="draft"]').click();
 await page.waitForFunction(()=>document.querySelector('[data-payroll-message]').textContent.includes('확정되지 않은 근태'));
 assert.equal(await page.locator('input[name="gross_pay"]').inputValue(),'21000');
 if(evidence)await page.screenshot({path:`${evidence}/payroll-${width}-workflow.png`,fullPage:true});
 assert.equal(await page.evaluate(()=>window.payrollCalls.filter(c=>/\/(confirm|pay|deliver)$/.test(c.path)).length),0);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({width,roster:'PASS',payroll:'PASS',fixture:true,productionWrites:0,pageErrors:0}));
 await page.close();
}}finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
